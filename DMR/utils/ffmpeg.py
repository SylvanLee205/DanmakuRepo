import copy
from datetime import datetime
import os
import subprocess
from os.path import join, abspath, dirname, exists
from .dataclass import VideoInfo
from .utils import get_tempfile, uuid
from .toolsmgr import ToolsList


def concat_video_ffmpeg(video_list:list, output:str):
    video_paths = []
    if isinstance(video_list[0], VideoInfo):
        for video in video_list:
            video_paths.append(abspath(video.path))
    else:
        video_paths = [abspath(video) for video in video_list]

    video_list_file = get_tempfile(suffix='txt')
    with open(video_list_file, 'w+', encoding='utf8') as f:
        for video in video_paths:
            f.write(f"file '{video}'\n")
    
    outdir = dirname(abspath(output))
    if not exists(outdir):
        os.makedirs(outdir)

    cmds = [ToolsList.get('ffmpeg'), '-y', '-f', 'concat', '-safe', '0', '-i', video_list_file, '-c', 'copy', output]
    ffmpeg_logfile = get_tempfile(suffix='log')
    start_time = datetime.now()
    with open(ffmpeg_logfile, 'w+', encoding='utf8') as f:
        proc = subprocess.Popen(cmds, stdin=subprocess.PIPE, stdout=f, stderr=subprocess.STDOUT)
        proc.wait()
        
        if proc.returncode != 0:
            f.seek(0)
            logs = f.read()
            raise Exception(f'Error when concat {video_paths} -> {output}: {logs[-100:]}, see {ffmpeg_logfile} for more details')
        
    os.remove(video_list_file)
    os.remove(ffmpeg_logfile)
    if isinstance(video_list[0], VideoInfo):
        output_info:VideoInfo = copy.deepcopy(video_list[0])
        output_info.dtype = 'dm_video'
        output_info.path = output
        output_info.file_id = uuid()
        output_info.size = os.path.getsize(output)
        output_info.ctime = start_time
        # ⚠️ 2026-11-02：不能直接 sum([v.duration for v in video_list])。
        #    VideoInfo.duration 的默认值是 **None**（dataclass.py:130），
        #    只要有一段时长未知，sum 就会抛 TypeError —— 而此时 ffmpeg 已经
        #    把文件合并完了，等于白做一遍（下次重试再炸一次）。
        #    另外 -1 是 ffprobe 的失败哨兵，累加会把总时长算小。
        #    这里只累加有效值；全都无效就保持 None（= 未知）。
        _durs = []
        for _v in video_list:
            try:
                _d = float(getattr(_v, 'duration', 0) or 0)
            except (TypeError, ValueError):
                _d = 0.0
            if _d > 0:
                _durs.append(_d)
        output_info.duration = sum(_durs) if _durs else None
        return output_info
    else:
        return output
