import json
from datetime import datetime
import logging
import os
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from os.path import join, exists, basename
from typing import Tuple

from DMR.utils import VideoInfo, DateTimeEncoder, DateTimeDecoder, uuid, PipeMessage


class Render():
    def __init__(self,
                 pipe:Tuple[queue.Queue, queue.Queue],
                 nrenders:int=1,
                 **kwargs,
                 ) -> None:
        
        self.nrenders = int(nrenders)
        self.send_queue, self.recv_queue = pipe
        self.logger = logging.getLogger(__name__)
        self.kwargs = kwargs
        self.stoped = True

        self._piperecvprocess = None
        self.render_tasks = {}
        self.failed_tasks = {}
        self.failed_tasks_file = '.temp/failed_renders.json'
        self.load_failed_tasks()

        self._render_class = {}
        self.render_executors = ThreadPoolExecutor(max_workers=self.nrenders)
        self._lock = threading.Lock()

    def load_failed_tasks(self):
        if exists(self.failed_tasks_file):
            try:
                with open(self.failed_tasks_file, 'r', encoding='utf-8') as f:
                    data = json.load(f, cls=DateTimeDecoder)
                    for uuid, task in data.items():
                        if task.get('video'):
                            task['video'] = VideoInfo(**task['video'])
                            # Update config as well
                            # if 'config' in task and 'video' in task['config']:
                            #     task['config']['video'] = task['video']
                        self.failed_tasks[uuid] = task
                self.logger.info(f'Loaded {len(self.failed_tasks)} failed render tasks.')
            except Exception as e:
                # ⚠️ 2026-11-02：加载失败必须把坏文件挪走。
                #    否则每次启动都报同一句错，而这份"待重试清单"实际上
                #    已经废了 —— 那些渲染失败的分段永远不会再被重试，
                #    但看起来只是"一个无关紧要的加载错误"。
                self.logger.error(f'Failed to load failed tasks: {e}')
                try:
                    bad = f'{self.failed_tasks_file}.corrupt-{datetime.now():%Y%m%d-%H%M%S}'
                    os.replace(self.failed_tasks_file, bad)
                    self.logger.error(f'损坏的待重试清单已另存为 {bad}（内容需人工检查）')
                except OSError:
                    pass

    def save_failed_tasks(self):
        """原子写入：先写临时文件再 replace，避免写一半崩溃留下半个 JSON。

        为什么重要：这份清单是"渲染失败、下次启动要重试"的唯一记录。
        直接 `open('w')` 写到一半断电，文件就是半截 JSON —— 下次启动
        load 失败，所有待重试分段**永久丢失**（而且没有任何报错会提示你）。
        """
        try:
            os.makedirs(os.path.dirname(self.failed_tasks_file) or '.', exist_ok=True)
            tmp = self.failed_tasks_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.failed_tasks, f, cls=DateTimeEncoder,
                          ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.failed_tasks_file)
        except Exception as e:
            self.logger.error(f'Failed to save failed tasks: {e}')

    def retry_task(self, uuid):
        with self._lock:
            if uuid in self.failed_tasks:
                task = self.failed_tasks.pop(uuid)
                self.save_failed_tasks()
                
                # Re-submit
                task['status'] = 'waiting'
                self.render_tasks[task['uuid']] = task
                self.render_executors.submit(self._render_subprocess, task)
                return True
            return False

    def delete_failed_task(self, uuid):
        with self._lock:
            if uuid in self.failed_tasks:
                self.failed_tasks.pop(uuid)
                self.save_failed_tasks()
                return True
            return False

    def _pipeSend(self, event, msg, target='engine', dtype=None, data=None, **kwargs):
        if self.send_queue:
            msg = PipeMessage(
                source='render',
                target=target,
                event=event,
                msg=msg,
                dtype=dtype,
                data=data,
                **kwargs,
            )
            self.send_queue.put(msg)

    def _pipeRecvMonitor(self):
        while self.stoped == False and self.recv_queue is not None:
            message:PipeMessage = self.recv_queue.get()
            try:
                if message.target == 'render':
                   if message.event == 'newtask':
                       self.add_task(message)
            except Exception as e:
                self.logger.error(f'Message:{message} raise an error.')
                self.logger.exception(e)
    
    def start(self):
        self.stoped = False
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

    def add_task(self, msg:PipeMessage):
        with self._lock:
            config = msg.data
            source = msg.source
            request_id = msg.request_id
            task = {
                'uuid': uuid(),
                'source': source,
                'request_id': request_id,
                'mode': config.get('mode', 'dmrender'),
                'args': config.get('args', {}),
                'video': config.get('video'),
                'output': config.get('output'),
                # 'config': config,
                'status': 'waiting',
            }
            self.render_tasks[task['uuid']] = task
            self.render_executors.submit(self._render_subprocess, task)

    def _gather(self, task, status, desc=''):
        with self._lock:
            self.render_tasks.pop(task['uuid'], None)
            if status == 'error':
                self.failed_tasks[task['uuid']] = task
                self.save_failed_tasks()

                self._pipeSend(
                    event='error',
                    msg=f"渲染视频{task['output']}时出现错误: {desc}",
                    target=task['source'],
                    request_id=task['request_id'],
                    dtype=str(type(desc)),
                    data=desc,
                )
            else:
                self._pipeSend(
                    event='end',
                    # 只用文件名，不带 "./直播回放/xxx/" 这种长路径 ——
                    # 插件那边会原样打印，带路径会又长又乱
                    msg=f"{basename(task['output'])} 渲染完成",
                    target=task['source'],
                    request_id=task['request_id'],
                    dtype='dict',
                    data={
                        # 'config': task['config'],
                        'output': desc,
                    },
                )

    def _render_subprocess(self, task):
        task['status'] = 'rendering'
        target_render = None
        try:
            render_args = task['args']
            mode:str = task['mode']
            video:VideoInfo = task.get('video')
            output:str = task.get('output')

            if mode == 'dmrender':
                from .dmrender import DmRender as TargetRender
            elif mode == 'transcode':
                from .transcode import Transcoder as TargetRender
            elif mode == 'rawffmpeg':
                raise NotImplementedError
                from .ffmpeg import RawFFmpegRender as TargetRender
            
            target_render = TargetRender(**render_args)
            self.logger.log(25, f'正在渲染: {basename(video.path)}')   # PROGRESS
            os.makedirs(os.path.dirname(output), exist_ok=True)

            self._render_class[task['uuid']] = target_render
            try:
                status, info = target_render.render_one(video=video, output=output)
            finally:
                # ⚠️ 2026-10-19：原来只有成功/失败两条正常路径会 pop，
                #    render_one 抛异常时条目会永久留在 _render_class 里
                #    （每次失败泄漏一个渲染器对象，且 stop() 会对死对象再 stop 一次）。
                self._render_class.pop(task['uuid'], None)

            if status:
                self._gather(task, 'info', desc=info)
            else:
                self._gather(task, 'error', desc=info)
        except KeyboardInterrupt:
            if target_render is not None:
                target_render.stop()
        except Exception as e:
            self.logger.exception(e)
            self._gather(task, 'error', desc=e)

    def stop(self):
        self.stoped = True
        # ⚠️ 2026-10-19：必须遍历快照。_render_subprocess 的 finally 会
        #    self._render_class.pop(...)，直接遍历原字典会撞上
        #    "dictionary changed size during iteration"，整个 stop() 抛异常 →
        #    下面的 failed_tasks 落盘被跳过（重启后这些任务就丢了）。
        for uuid, render in list(self._render_class.items()):
            try:
                render.stop()
            except Exception as e:                            # noqa: BLE001
                self.logger.warning(f'停止渲染器 {uuid} 时出错: {e}')
        self.render_executors.shutdown(wait=False)
        
        with self._lock:
            for uuid, task in self.render_tasks.items():
                if uuid not in self.failed_tasks:
                    self.failed_tasks[uuid] = task
            self.save_failed_tasks()

        self.logger.info('Render stopped.')
