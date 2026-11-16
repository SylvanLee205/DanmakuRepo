import subprocess
import json
import warnings
import re

from .toolsmgr import ToolsList

class FFprobe():
    header = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) AppleWebKit/537.36 '
                                '(KHTML, like Gecko) Chrome/75.0.3770.100 Mobile Safari/537.36 '
            }

    @classmethod
    def ffprobe(cls) -> str:
        return ToolsList.get('ffprobe')
    
    @classmethod
    def run_ffprobe(cls, fpath, timeout: float = 60):
        """跑 ffprobe 读容器/流信息。

        ⚠️ 2026-11-16：加 timeout。这里在**渲染线程池**里被调用，遇到损坏
        文件或网络盘挂起时，无超时的 check_output 会永久占住一个渲染线程；
        线程池满之后后续所有渲染都排队，表现为"渲染不动了但进程还活着"。
        """
        out = subprocess.check_output([
            cls.ffprobe(),
            '-i', fpath,
            '-print_format','json',
            '-select_streams', 'v:0',
            '-show_format','-show_streams',
            '-v','quiet'
            ], timeout=timeout)
        out = out.decode('utf8')
        res = json.loads(out)
        return res
        
    @classmethod
    def get_duration(cls,fpath) -> float:
        try:
            res = cls.run_ffprobe(fpath)
            try:
                st = float(res['format']['start_time'])
            except (KeyError, TypeError, ValueError):
                st = 0
            duration = float(res['format']['duration'])-st
            return max(duration, 0)
        except subprocess.TimeoutExpired:
            warnings.warn(f'ffprobe 读取超时（{fpath}）')
            return -1
        except Exception:                                        # noqa: BLE001
            return -1

    @classmethod
    def get_media_duration(cls, fpath, timeout=300) -> float:
        """按视频包时长求和，得到「可解码媒体时长」（秒），失败返回 -1。

        为什么需要这个口径（2026-11-16）：
            ``get_duration()`` 读的是 ``format.duration``，那是**时间轴跨度**，
            包含断流、卡顿、重连留下的空洞；而渲染时 ``-fflags +genpts``
            会把空洞压掉，产物时长对应的是**真正解码出来的画面总长**。
            拿跨度当基准，就会把完整产物判成残缺：
              实测 vvu 11-07 14:19：跨度 3601s，中间 21 处缺口共 514s，
              实际可解码 3060s，产物 3096s —— 被判成 86% 误删。
            视频包 ``duration_time`` 求和 ≈ 可解码总长，与产物语义一致。

        :returns: 秒数；拿不到有效包时长时返回 -1（调用方需自行兜底）。
        """
        try:
            out = subprocess.check_output(
                [
                    cls.ffprobe(),
                    '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'packet=duration_time',
                    '-of', 'csv=p=0',
                    fpath,
                ],
                timeout=timeout,
            )
        except Exception:
            return -1

        total = 0.0
        count = 0
        for line in out.decode('utf8', 'replace').splitlines():
            value = line.strip().strip(',')
            if not value or value in ('N/A', 'nan', 'NaN'):
                continue
            try:
                total += float(value)
                count += 1
            except ValueError:
                continue
        if count <= 0 or total <= 0:
            return -1
        return total

    @classmethod
    def run_ffprobe_livestream(cls, url, header=None):
        if header is None:
            header = cls.header
        out = subprocess.check_output([
            cls.ffprobe(),
            '-headers', ''.join('%s: %s\r\n' % x for x in header.items()),
            '-i', url,
            '-select_streams', 'v:0', 
            '-print_format','json',
            '-show_format','-show_streams',
            '-v','quiet'
            ],
            timeout=15,
        )
        out = out.decode('utf8')
        res = json.loads(out)
        return res

    @classmethod
    def get_livestream_info(cls,url,header=None) -> dict:
        res = cls.run_ffprobe_livestream(url,header)
        return res['streams'][0]
        
    @classmethod
    def get_resolution(cls, url:str, header=None) -> tuple:
        try:
            if url.startswith('http'):
                res = cls.run_ffprobe_livestream(url, header)
            else:
                res = cls.run_ffprobe(url)
            resolution = res['streams'][0]['width'],res['streams'][0]['height']
            return resolution
        except Exception:                                        # noqa: BLE001
            return 0,0

    @classmethod
    def get_fps(cls, url:str, header=None, fallback:float=0.0) -> float:
        """获取视频帧率（浮点数）。

        优先使用 r_frame_rate（真实基础帧率），失败时回退到 avg_frame_rate。
        获取不到时返回 fallback（默认 0，调用方需要自己决定兜底值）。
        """
        try:
            if str(url).startswith('http'):
                res = cls.run_ffprobe_livestream(url, header)
            else:
                res = cls.run_ffprobe(url)
            stream = res['streams'][0]

            for key in ('r_frame_rate', 'avg_frame_rate'):
                rate = stream.get(key)
                if not rate or rate in ('0/0', '0'): 
                    continue
                num, _, den = str(rate).partition('/')
                den = den or '1'
                try:
                    fps = float(num) / float(den)
                except (ZeroDivisionError, ValueError):
                    continue
                if fps > 0:
                    return fps
        except Exception:
            pass
        return fallback