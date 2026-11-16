import os
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

    #: ``probe_status`` 的三种结论
    PROBE_OK = 'ok'                   #: 解析成功，detail 是容器时长（秒）
    PROBE_UNREADABLE = 'unreadable'   #: ffprobe 能跑，但确实读不了这个文件（缺 moov / 非媒体文件）
    PROBE_UNAVAILABLE = 'unavailable'  #: 探测本身不成立（没装 ffprobe / 超时 / 权限），调用方必须 fail-open

    @classmethod
    def probe_status(cls, fpath, timeout: float = 60):
        """三态探测：文件可读 / 文件读不了 / 探测本身不可用。

        为什么必须区分（2026-11-16 第二轮复审 N-2）：
            ``get_duration`` 与 ``get_media_duration`` 把所有异常都吞成 ``-1``，
            调用方根本分不清"这台机器没装 ffprobe / 超时"和"这个 mp4 真的没有
            moov"。而恢复流程的判断是「读不出时长 → 删掉重新渲染」——
            在探测本身失效的那一天，它会**批量删掉好成品**。

        判据来源可靠：ffprobe 正常跑起来但文件读不了时，退出码非 0 且 stderr
        有具体信息（``moov atom not found`` / ``Invalid data found``）；
        没装 / 超时 / 无权限则在启动子进程阶段就抛异常，两者不会混淆。

        :returns: ``(status, detail)``
            * ``('ok', 时长秒数)``
            * ``('unreadable', 错误文本)``
            * ``('unavailable', 原因)``
        """
        try:
            exe = cls.ffprobe()
        except Exception as e:                                   # noqa: BLE001
            return cls.PROBE_UNAVAILABLE, f'找不到 ffprobe: {type(e).__name__}: {e}'
        try:
            proc = subprocess.run(
                [exe, '-v', 'error',
                 '-show_entries', 'format=duration',
                 '-of', 'default=nw=1:nk=1', str(fpath)],
                capture_output=True, text=True, timeout=timeout,
                encoding='utf-8', errors='replace',
            )
        except FileNotFoundError as e:
            return cls.PROBE_UNAVAILABLE, f'ffprobe 不存在: {e}'
        except subprocess.TimeoutExpired:
            return cls.PROBE_UNAVAILABLE, f'ffprobe 探测超时（{timeout}s）'
        except OSError as e:
            return cls.PROBE_UNAVAILABLE, f'ffprobe 无法启动: {type(e).__name__}: {e}'
        except Exception as e:                                   # noqa: BLE001
            return cls.PROBE_UNAVAILABLE, f'ffprobe 异常: {type(e).__name__}: {e}'

        if proc.returncode != 0:
            err = (proc.stderr or '').strip().replace('\n', ' ')[-300:]
            return cls.PROBE_UNREADABLE, err or f'ffprobe 退出码 {proc.returncode}'
        try:
            return cls.PROBE_OK, float((proc.stdout or '').strip().splitlines()[0])
        except (ValueError, IndexError):
            err = (proc.stderr or '').strip().replace('\n', ' ')[-300:]
            return cls.PROBE_UNREADABLE, err or 'ffprobe 未返回时长'

    @classmethod
    def is_playable(cls, path) -> bool:
        """这个文件是否**像个能播的媒体**（0 字节 / 缺索引的半截 mp4 都算不能）。

        用途：所有"看到成品就允许删源"的闸门（清理工具、磁盘紧急清理）
        都必须先过这一关，否则一个 0 字节或被中途杀掉的成品就能让源视频
        （唯一原始画质副本）被删掉。

        探测工具不可用时返回 ``True``（放行）：环境问题不该被当成"文件坏了"，
        否则 ffprobe 一缺，整个恢复/清理流程会集体误判。
        """
        try:
            if not path or not os.path.exists(path):
                return False
            if os.path.getsize(path) <= 0:
                return False
        except OSError:
            return False
        if str(path).lower().endswith(('.mp4', '.mov', '.m4v')):
            status, _detail = cls.probe_status(path)
            if status == cls.PROBE_UNREADABLE:
                return False
        return True

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