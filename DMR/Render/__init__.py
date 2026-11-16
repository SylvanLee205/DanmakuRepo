import json
from datetime import datetime
import logging
import os
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from os.path import join, exists, basename
from typing import Tuple

from DMR.utils import VideoInfo, DateTimeEncoder, DateTimeDecoder, uuid, PipeMessage, FFprobe


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

    @staticmethod
    def _video_key(task) -> str:
        """失败任务的去重键：同一个源视频只保留一条待重试记录。

        2026-11-16：原来只按 uuid 去重，同一个分段反复失败就会在
        ``.temp/failed_renders.json`` 里堆出多条（实测 16 条里有 5 组重复）。
        重复条目不仅让 Web 界面的"失败渲染"列表虚高，重启后的重试还会
        对同一个源视频并发渲染，白占 GPU。Windows 路径大小写不敏感，
        所以统一走 normcase + abspath。
        """
        video = task.get('video') if isinstance(task, dict) else None
        if isinstance(video, dict):
            path = video.get('path')
        else:
            path = getattr(video, 'path', None)
        if not path:
            return ''
        try:
            return os.path.normcase(os.path.abspath(str(path)))
        except OSError:
            return str(path)

    def _output_looks_complete(self, task, video) -> bool:
        """产物已存在且时长和源对得上 → 这条"失败"记录其实已经过期。

        为什么需要（2026-11-16）：重启时 reconciler 会用**新 uuid** 补派
        渲染，成功之后旧失败条目没人清，界面上永远显示"失败"，手工点重试
        还会白渲一遍。实测 10 条里有 5 条产物已经完整落盘。

        判据沿用渲染闸门：产物时长 ≥ 源可解码时长的 90%。只有 ``output``
        确实存在时才探测（绝大多数失败条目没有产物，不会付这份开销）。
        """
        out = task.get('output')
        if not out or not os.path.exists(str(out)):
            return False
        src = getattr(video, 'path', None)
        if not src:
            return False
        try:
            out_dur = float(FFprobe.get_duration(out) or 0)
            if out_dur <= 0:
                return False
            src_dur = float(FFprobe.get_media_duration(str(src)) or -1)
            if src_dur <= 0:
                return False
            return out_dur >= src_dur * 0.90
        except Exception:
            return False

    def load_failed_tasks(self):
        if exists(self.failed_tasks_file):
            try:
                with open(self.failed_tasks_file, 'r', encoding='utf-8') as f:
                    data = json.load(f, cls=DateTimeDecoder)
                    dropped = 0
                    duplicated = 0
                    stale = 0
                    seen_paths = {}
                    for uuid, task in data.items():
                        if task.get('video'):
                            # ⚠️ 2026-11-03：源视频已不在磁盘上的条目是**僵尸**。
                            #   怎么产生的：用户把录播归档走了，或目录结构迁移过
                            #   （2026-11-02 换成 原视频/ + 弹幕版/ 布局）。
                            #   为什么必须在这里清掉：
                            #     ① 留着它们，Web 界面的"失败渲染"列表永远显示
                            #        一堆已经不存在的东西；
                            #     ② 更糟 —— save_failed_tasks() 每次都会把内存里
                            #        这份清单**原样写回文件**，所以手工清空文件
                            #        也会在下次保存时被写回来，永远清不掉。
                            #   判据：video 里的 path 在磁盘上不存在 → 丢弃。
                            raw = task['video']
                            path = raw.get('path') if isinstance(raw, dict) \
                                else getattr(raw, 'path', None)
                            if path and not os.path.exists(path):
                                dropped += 1
                                continue
                            task['video'] = VideoInfo(**raw)
                            # Update config as well
                            # if 'config' in task and 'video' in task['config']:
                            #     task['config']['video'] = task['video']
                            # 产物已经完整落盘 → 这条失败记录过期（补派成功后的残留）
                            if self._output_looks_complete(task, task['video']):
                                stale += 1
                                continue
                            # ⚠️ 2026-11-16：同一源视频只留最新一条（json 里
                            #   越靠后越新），旧条目直接顶掉并落盘。
                            key = self._video_key(task)
                            if key:
                                old = seen_paths.get(key)
                                if old is not None:
                                    self.failed_tasks.pop(old, None)
                                    duplicated += 1
                                seen_paths[key] = uuid
                        self.failed_tasks[uuid] = task
                if dropped or duplicated or stale:
                    # 立刻落盘，把僵尸/重复/过期条目从文件里真正抹掉
                    if dropped:
                        self.logger.info(f'丢弃 {dropped} 条源视频已不存在的失败渲染记录'
                                         f'（录播被归档或目录迁移所致）。')
                    if duplicated:
                        self.logger.info(f'合并 {duplicated} 条重复的失败渲染记录'
                                         f'（同一源视频只保留最新一条）。')
                    if stale:
                        self.logger.info(f'清理 {stale} 条过期失败记录'
                                         f'（产物已完整落盘，属补派成功后的残留）。')
                    self.save_failed_tasks()
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
                # 同一源视频只保留最新一条失败记录，避免反复重试堆成一片
                key = self._video_key(task)
                if key:
                    for old_uuid, old_task in list(self.failed_tasks.items()):
                        if self._video_key(old_task) == key:
                            self.failed_tasks.pop(old_uuid, None)
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
                # 同一源视频现在成功了，之前的失败记录就过期了。
                # 场景：重启时由 reconciler 补派的分段走的是**新 uuid**，
                # 旧失败条目留在 failed_tasks 里没人清（实测 5 条已经渲染
                # 成功却仍显示"失败"，手工点重试会白渲一遍）。
                key = self._video_key(task)
                if key:
                    removed = False
                    for old_uuid, old_task in list(self.failed_tasks.items()):
                        if self._video_key(old_task) == key:
                            self.failed_tasks.pop(old_uuid, None)
                            removed = True
                    if removed:
                        self.save_failed_tasks()
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
        
        # 2026-11-16 修（审查报告 L-8）：原来把 render_tasks 里**所有**任务都抄进
        # failed_tasks，包括正在渲染的。shutdown(wait=False) 不保证它们已停下，
        # 工作线程可能随后渲染成功；清单里却留下失败记录 → 人工重试白渲一遍，
        # 若成品已被上传，还会出现"重复渲染同一分段"。
        # 只收"还没开始"的任务；在跑的交给状态库（RENDERING → 重启对账降级为
        # RECORDED → 自动恢复或重渲），不做主观判断。
        with self._lock:
            for task_uuid, task in list(self.render_tasks.items()):
                if task.get('status') != 'waiting':
                    continue
                if task_uuid not in self.failed_tasks:
                    self.failed_tasks[task_uuid] = task
            self.save_failed_tasks()

        self.logger.info('Render stopped.')
