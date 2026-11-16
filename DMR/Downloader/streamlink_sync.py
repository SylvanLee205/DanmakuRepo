import logging
import queue
import subprocess
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED

from DMR.utils import ToolsList


def _err_text(errfile, limit:int=800) -> str:
    """从错误日志临时文件里读回最后 limit 个字符。

    子进程的 stderr 重定向到临时文件而不是 PIPE（见 L-12 注释），
    读文件不会阻塞，也不会因为管道写满把子进程卡死。
    """
    try:
        errfile.seek(0)
        data = errfile.read()
        if isinstance(data, bytes):
            data = data.decode('utf-8', errors='replace')
        return (data or '')[-limit:]
    except Exception:
        return ''


class StreamlinkSyncDownloader():
    def __init__(self,  
                 segment:int,
                 url:str,
                 taskname:str,
                 segment_callback=None,
                 **kwargs):

        self.segment = segment
        self.taskname = taskname
        self.url = url
        self.segment_callback = segment_callback
        self.kwargs = kwargs

        self.advanced_video_args = kwargs.get('advanced_video_args', {})
        self.logger = logging.getLogger(__name__)
        self.read_block_size = 256*1024
        self.pool = None
        self.futures = deque(maxlen=2)
        self.stoped = False
    
    def start_helper(self):
        self.logger.info(f'{self.taskname} 同步录制开始.')
        streamlink_extra_args = self.advanced_video_args.get('streamlink_extra_args') or []
        streamlink_extra_args = self.advanced_video_args.get('streamlink_extra_args') or []
        streamlink_quality = self.advanced_video_args.get('streamlink_quality') or 'best'
        streamlink_args = [
            "streamlink",
            *streamlink_extra_args,
            '-O',
            self.url,
            streamlink_quality,
        ]
        self.logger.debug(f'{self.taskname} streamlink args: {streamlink_args}')

        ffmpeg_args = [
            ToolsList.get('ffmpeg'),
            "-fflags", "+genpts",
            "-i", 'pipe:0',
            "-c:v", "copy",
            "-c:a", "copy",
            # 2026-11-16 修（审查报告 L-12）：这里原来还有 "-reset_timestamps","1"。
            # 该选项只属于 segment muxer，和下面的 "-f flv" 一起用会让 ffmpeg
            # 启动即报 "Option not found"，同步录制模式一次都起不来（已用
            # `ffmpeg -h muxer=segment` 核对过归属）。时间戳归一化交给
            # -avoid_negative_ts 就够了。
            "-avoid_negative_ts", "1",
            "-movflags", "+frag_keyframe+empty_moov",
            "-f", "flv",
            "-",
        ]
        self.logger.debug(f'{self.taskname} ffmpeg args: {ffmpeg_args}')
        wait_to_stop = False

        # 2026-11-16 修（审查报告 L-12）：两个子进程原来都是 stderr=PIPE
        # 而**全程不读**，输出写满管道（Windows 约 64KB）后子进程阻塞在写、
        # 父进程阻塞在读 stdout，双向死锁。现在各自重定向到独立临时文件
        # （Windows 上父子共享同一 handle 会共享文件指针，所以必须开两份）。
        sl_err = tempfile.TemporaryFile(prefix='streamlink_err_')
        ff_err = tempfile.TemporaryFile(prefix='ffmpeg_err_')

        with subprocess.Popen(streamlink_args, stdout=subprocess.PIPE, stderr=sl_err) as streamlink_proc:
            with subprocess.Popen(ffmpeg_args, stdin=streamlink_proc.stdout, stdout=subprocess.PIPE, stderr=ff_err) as ffmpeg_proc:
                # 读取第一个数据
                data = ffmpeg_proc.stdout.read(self.read_block_size)
                if not data:  # 如果第一个数据为空
                    self.logger.error(f"{self.taskname}: ffmpeg启动失败")
                    streamlink_proc.kill()  # 终止 streamlink 进程
                    ffmpeg_proc.kill()  # 终止 ffmpeg 进程
                    # 打印错误输出
                    ffmpeg_err = _err_text(ff_err)
                    if ffmpeg_err:
                        self.logger.error(f"{self.taskname}: ffmpeg启动失败 " + ffmpeg_err)
                    streamlink_err = _err_text(sl_err)
                    if streamlink_err:
                        self.logger.error(f"{self.taskname}: streamlink启动失败 " + streamlink_err)
                    return False
                
                segment_start_time = time.time()
                video_queue = queue.SimpleQueue()
                self.segment_callback(video_queue)
                video_queue.put(data)  # 将第一个数据放入队列

                # 循环读取
                while not self.stoped:
                    data = ffmpeg_proc.stdout.read(self.read_block_size)
                    if not data:
                        self.logger.info(f"{self.taskname}: ffmpeg 进程结束")
                        ffmpeg_err = _err_text(ff_err, 300)
                        if ffmpeg_err:
                            self.logger.debug(f"{self.taskname} ffmpeg: " + ffmpeg_err)
                        streamlink_err = _err_text(sl_err, 300)
                        if streamlink_err:
                            self.logger.debug(f"{self.taskname} streamlink: " + streamlink_err)
                        break

                    if not wait_to_stop and time.time() - segment_start_time > self.segment - 10:
                        # 提前10秒准备下一个分片
                        future = self.pool.submit(self.start_helper)
                        self.futures.append(future)
                        wait_to_stop = True
                    elif wait_to_stop and time.time() - segment_start_time > self.segment:
                        self.logger.info(f"{self.taskname}: 分片录制完成")
                        break

                    if video_queue.qsize() * self.read_block_size > 10*1024*1024:
                        self.logger.error(f"{self.taskname}: 输出阻塞")
                        streamlink_proc.kill()
                        ffmpeg_proc.kill()
                        # 清空队列
                        try:
                            while video_queue.qsize() > 0:
                                video_queue.get_nowait()
                        except queue.Empty:
                            pass
                        break

                    video_queue.put(data)

                ffmpeg_proc.kill()
                streamlink_proc.kill()
                video_queue.put(None)

                ffmpeg_err = _err_text(ff_err)
                if ffmpeg_err:
                    self.logger.debug(f"{self.taskname} ffmpeg output: " + ffmpeg_err)
                streamlink_err = _err_text(sl_err)
                if streamlink_err:
                    self.logger.debug(f"{self.taskname} streamlink output: " + streamlink_err)
                
        return True

    def start(self):
        self.pool = ThreadPoolExecutor(max_workers=2)
        future = self.pool.submit(self.start_helper)
        self.futures.append(future)
        while not self.stoped and len(self.futures) > 0:
            future = self.futures.popleft()
            future.result()
    
    def stop(self):
        self.stoped = True
        self.logger.debug('Streamlink sync downloader stoped.')