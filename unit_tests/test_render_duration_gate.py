"""渲染产物时长校验 —— 口径回归测试（2026-11-16）。

背景（实测复盘）：
    原来的闸门拿「弹幕最后时间戳 / VideoInfo.duration / 容器时长」当参考，
    这三个都是**墙钟时间轴**（含起流等待、卡顿、断流重连的空转）；而产物是
    **媒体时间轴**（``-fflags +genpts`` 已把时间戳空洞压掉）。于是完整成品
    被按比例判死并删除：

      * vvu 11-07 14:19：跨度 3601s，21 处缺口共 514s，可解码 3060s，
        产物 3096s（101%）—— 被当成 86% 删掉；
      * 君君 11-10 19:42：容器 168.3s，产物 168s —— 被当成 74% 删掉；
      * SFault 11-11 22:05：容器 127.9s，产物 128s —— 被当成 83% 删掉。

修法：
    参考值改用「视频包时长求和」= 可解码媒体时长；判定式
    ``缺口 > max(3 秒, 参考时长 * 10%)`` 才算残缺。10-18 那次 53% 的真残缺
    （缺口 1764 秒）依然拦得住。

另有两个同批修复在此一并锁住：
    1. XML 弹幕转出的临时 ``.ass`` 会在渲染结束的 ``finally`` 里被删，
       弹幕时间戳必须在**渲染前**读好，否则必然回退到墙钟口径；
    2. ``failed_renders.json`` 按源视频路径去重（同一分段反复失败会堆多条）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from DMR.Render import Render
from DMR.Render.dmrender import DmRender

from unit_tests.support import DMRTestCase


class _Video:
    """render_one 需要的最小 VideoInfo 替身（必须可 deepcopy）。"""

    def __init__(self, path, duration=0):
        self.path = path
        self.duration = duration
        self.file_id = 'src-1'
        self.dm_file_id = None
        self.dtype = 'src_video'
        self.size = 1
        self.ctime = None
        self.src_video_id = None


class _FakeRender(DmRender):
    """只替换真正调 ffmpeg 的那一步，产物写个小文件。"""

    def render_helper(self, video, danmaku, output, vencoder=None,
                      vencoder_args=None, **kwargs):
        with open(output, 'wb') as f:
            f.write(b'\0' * 2048)
        return True, 'video:2kB'


def _make_render(**overrides):
    params = dict(
        hwaccel_args=[], vencoder='hevc_qsv',
        vencoder_args=['-global_quality:v', '30'],
        aencoder='libopus', aencoder_args=['-b:a', '96k'],
        output_resize='', fallback_vencoder='libx265',
        fallback_vencoder_args=['-crf', '28'],
        hw_retry=0, hw_retry_delay=0, gop_multiplier=None,
    )
    params.update(overrides)
    return _FakeRender(**params)


class TestDurationGate(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='dmr_gate_')
        self.src = os.path.join(self.tmp, 'seg.ts')
        with open(self.src, 'wb') as f:
            f.write(b'\0' * 1024)
        self.dm = os.path.join(self.tmp, 'seg.ass')
        with open(self.dm, 'w', encoding='utf-8') as f:
            f.write('[Script Info]\n')
        self.out = os.path.join(self.tmp, 'out.mp4')

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self, product_dur, container_dur, media_dur,
                wall_dur=0.0, dm_ts=0.0):
        video = _Video(self.src, wall_dur)
        video.dm_file_id = self.dm

        def fake_get_duration(path):
            if os.path.abspath(str(path)) == os.path.abspath(self.out):
                return product_dur
            return container_dur

        with mock.patch('DMR.Render.dmrender.FFprobe.get_duration',
                        side_effect=fake_get_duration), \
                mock.patch('DMR.Render.dmrender.FFprobe.get_media_duration',
                           return_value=media_dur), \
                mock.patch.object(DmRender, '_danmaku_last_timestamp',
                                  staticmethod(lambda _p: dm_ts)):
            return _make_render().render_one(video, self.out)

    # ── 1. 完整成品不能被墙钟参考误杀 ──────────────────────────────
    def test_product_with_timestamp_gaps_is_kept(self) -> None:
        """vvu 复现：容器/墙钟 3601s，可解码 3060s，产物 3096s → 通过。"""
        status, _ = self._render(product_dur=3096, container_dur=3601,
                                 media_dur=3060, wall_dur=3601)
        self.assertTrue(status, '产物与可解码时长一致，不应判为残缺')
        self.assertTrue(os.path.exists(self.out), '完整产物不能被删除')

    def test_short_segment_kept_when_durations_match(self) -> None:
        """君君/SFault 复现：容器 168.3s，产物 168s → 通过。"""
        status, _ = self._render(product_dur=168, container_dur=168.3,
                                 media_dur=168.2, wall_dur=227)
        self.assertTrue(status)
        self.assertTrue(os.path.exists(self.out))

    # ── 2. 真正的截断必须继续拦住 ─────────────────────────────────
    def test_real_truncation_is_rejected(self) -> None:
        """断尾人类 10-18 复现：源 3791s，产物只有 2027s（53%）→ 失败并删产物。"""
        status, info = self._render(product_dur=2027, container_dur=3791,
                                    media_dur=3791, wall_dur=3791)
        self.assertFalse(status, '53% 的残缺产物必须判失败')
        self.assertIn('时长异常', str(info))
        self.assertFalse(os.path.exists(self.out), '残缺产物应被删除')

    def test_short_segment_truncation_is_rejected(self) -> None:
        """100 秒分段只剩 85 秒（缺口 15s > 容差 10s）→ 失败。"""
        status, _ = self._render(product_dur=85, container_dur=100,
                                 media_dur=100, wall_dur=100)
        self.assertFalse(status)

    # ── 3. 临时 ass 必须在删除前读取 ──────────────────────────────
    def test_temporary_ass_is_read_before_cleanup(self) -> None:
        """XML 弹幕场景：临时 .ass 在 finally 被删，时间戳必须提前读好。

        旧实现在闸门里才读，此时文件已没了 → 返回 0 → 静默回退墙钟口径，
        既可能误杀也可能放过。这里让产物(100s)明显短于弹幕(300s)，
        只有"提前读到 300s"才会判失败。
        """
        ass = os.path.join(self.tmp, 'temp_from_xml.ass')
        with open(ass, 'w', encoding='utf-8') as f:
            f.write('[Script Info]\n')
            f.write('Dialogue: 0,0:05:00.00,0:05:01.00,Default,,0,0,0,,hello\n')

        video = _Video(self.src, 0)
        video.dm_file_id = self.dm

        def fake_get_duration(path):
            if os.path.abspath(str(path)) == os.path.abspath(self.out):
                return 100.0
            return -1.0

        with mock.patch('DMR.Render.dmrender.FFprobe.get_duration',
                        side_effect=fake_get_duration), \
                mock.patch('DMR.Render.dmrender.FFprobe.get_media_duration',
                           return_value=-1.0), \
                mock.patch.object(DmRender, '_prepare_danmaku',
                                  return_value=(ass, ass)):
            status, _ = _make_render().render_one(video, self.out)

        self.assertFalse(status, '应按弹幕最后时间戳(300s)判定为截断')
        self.assertFalse(os.path.exists(ass), '临时 ass 仍应被清理')


class TestFailedRenderDedup(DMRTestCase):
    """failed_renders.json 同一源视频只保留一条（2026-11-16）。"""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='dmr_dedup_')
        self._cwd = os.getcwd()
        os.chdir(self.tmp)
        os.makedirs('.temp', exist_ok=True)
        self.src = os.path.join(self.tmp, 'seg.ts')
        with open(self.src, 'wb') as f:
            f.write(b'\0' * 16)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    @staticmethod
    def _entry(path, **extra):
        data = {'video': {'path': path, 'file_id': 'f1', 'dtype': 'src_video',
                          'duration': 60}, 'status': 'rendering',
                'output': path + '.mp4', 'mode': 'dmrender'}
        data.update(extra)
        return data

    def _write(self, payload):
        with open('.temp/failed_renders.json', 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)

    def test_duplicate_paths_are_merged(self) -> None:
        self._write({
            'aaa': self._entry(self.src),
            'bbb': self._entry(self.src),
            'ccc': self._entry(os.path.join(self.tmp, 'gone.ts')),
        })

        render = Render(pipe=(None, None))
        render.render_executors.shutdown(wait=False)

        self.assertEqual(len(render.failed_tasks), 1,
                         '同一源视频应合并为一条，僵尸条目应丢弃')
        self.assertIn('bbb', render.failed_tasks, '应保留最新一条（json 中靠后）')

        with open('.temp/failed_renders.json', encoding='utf-8') as f:
            saved = json.load(f)
        self.assertEqual(list(saved), ['bbb'], '去重结果必须立刻落盘')

    def test_video_key_is_case_insensitive(self) -> None:
        upper = self.src.upper()
        self.assertEqual(Render._video_key(self._entry(self.src)),
                         Render._video_key(self._entry(upper)))

    def test_stale_failure_dropped_when_output_complete(self) -> None:
        """产物已完整落盘 → 旧的"失败"记录是补派成功后的残留，应清掉。"""
        out = self.src + '.mp4'
        with open(out, 'wb') as f:
            f.write(b'\0' * 32)
        self._write({'old': self._entry(self.src)})

        with mock.patch('DMR.Render.FFprobe.get_duration', return_value=100.0), \
                mock.patch('DMR.Render.FFprobe.get_media_duration', return_value=100.0):
            render = Render(pipe=(None, None))
        self.assertEqual(render.failed_tasks, {})
        render.render_executors.shutdown(wait=False)

    def test_stale_failure_kept_when_output_truncated(self) -> None:
        """产物存在但只有 50% → 这是真失败，记录必须保留以便重试。"""
        out = self.src + '.mp4'
        with open(out, 'wb') as f:
            f.write(b'\0' * 32)
        self._write({'old': self._entry(self.src)})

        with mock.patch('DMR.Render.FFprobe.get_duration', return_value=50.0), \
                mock.patch('DMR.Render.FFprobe.get_media_duration', return_value=100.0):
            render = Render(pipe=(None, None))
        self.assertEqual(len(render.failed_tasks), 1)
        render.render_executors.shutdown(wait=False)

    def test_success_clears_stale_failure(self) -> None:
        """同一源视频渲染成功后，旧失败记录必须被清掉。

        场景：重启时 reconciler 用**新 uuid** 补派，旧条目没人清，
        界面会一直显示"失败"，手工重试等于白渲一遍。
        """
        from DMR.utils import VideoInfo

        self._write({'old': self._entry(self.src)})
        render = Render(pipe=(None, None))
        self.assertEqual(len(render.failed_tasks), 1)

        render._gather(
            {'uuid': 'new', 'source': 'seg', 'request_id': 'r1',
             'output': self.src + '.mp4',
             'video': VideoInfo(path=self.src, file_id='f1')},
            'info', desc=self.src + '.mp4')

        self.assertEqual(render.failed_tasks, {}, '成功后旧失败记录应被清除')
        with open('.temp/failed_renders.json', encoding='utf-8') as f:
            self.assertEqual(json.load(f), {})
        render.render_executors.shutdown(wait=False)


if __name__ == '__main__':
    unittest.main()
