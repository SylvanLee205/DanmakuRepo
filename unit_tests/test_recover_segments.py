"""孤儿分段恢复工具的配对逻辑（2026-11-16 第二轮复审 N-4）。

背景：这个工具用来把"进程被杀后留下的 ``[正在录制]xxx-Part000.ts``" 登记成正式
文件并重新派发渲染。它靠**文件名里的时间戳**把视频和弹幕配成一对。

上一版"修复"把配对键写成了 ``re.findall(r'\\d{14}', name)`` —— 要求 14 位数字
**连续**。而真实命名是 ``YYYYMMDD-HHMMSS``（带连字符，工具自己的分段正则都是
``\\d{8}-\\d{6}``），于是所有候选都被跳过、``find_danmaku`` 恒返回 None：
孤儿分段恢复时永远"没找到配对弹幕"，渲染必失败。这里把两个方向都锁住。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE_PATH = os.path.join(_ROOT, 'tools', 'recover_ffmpeg_segments.py')


def load_module():
    spec = importlib.util.spec_from_file_location('recover_segments_under_test',
                                                  _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestToken14(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_hyphenated_timestamp_is_parsed(self):
        """★回归 N-4：真实命名带连字符，必须能取出时间戳。"""
        self.assertEqual(
            self.m._token14_of('[正在录制]主播-20261018-231500-Part000.ass'),
            datetime(2026, 10, 18, 23, 15, 0))

    def test_continuous_digits_still_work(self):
        """历史上也有连续 14 位的写法，不能改坏。"""
        self.assertEqual(self.m._token14_of('主播-20261018231500.ts'),
                         datetime(2026, 10, 18, 23, 15, 0))

    def test_digits_in_streamer_name_do_not_confuse(self):
        """主播名里带数字时，滑动窗口仍应取到合法日期。"""
        self.assertEqual(self.m._token14_of('12345主播-20261018-231500-Part001.ts'),
                         datetime(2026, 10, 18, 23, 15, 0))

    def test_no_timestamp_returns_none(self):
        self.assertIsNone(self.m._token14_of('主播-Part000.ts'))
        self.assertIsNone(self.m._token14_of(''))


class TestFindDanmaku(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dmr_pair_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _touch(self, name):
        p = os.path.join(self.tmp, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('[Script Info]\n')
        return p

    def test_pairs_hyphenated_danmaku(self):
        """★核心：``[正在录制]...-HHMMSS-Part00N`` 命名必须配对成功。"""
        seg = self._touch('[正在录制]主播-20261018-231500-Part000.ts')
        ass = self._touch('[正在录制]主播-20261018-231500-Part000.ass')

        got = self.m.find_danmaku(seg, '20261018-231500', part_idx=0)

        self.assertEqual(got, ass, '带连字符的时间戳配不上 → 恢复时渲染必失败')

    def test_prefers_same_part_number(self):
        """Part001 不该配到 Part000 的弹幕（会把别人的弹幕搬走）。"""
        seg = self._touch('[正在录制]主播-20261018-231500-Part001.ts')
        self._touch('[正在录制]主播-20261018-231500-Part000.ass')
        want = self._touch('[正在录制]主播-20261018-231501-Part001.ass')

        got = self.m.find_danmaku(seg, '20261018-231500', part_idx=1)

        self.assertEqual(got, want)

    def test_timestamp_beyond_tolerance_is_rejected(self):
        seg = self._touch('[正在录制]主播-20261018-231500-Part000.ts')
        self._touch('别的场次-20261018-100000.ass')     # 差 13 小时

        self.assertIsNone(self.m.find_danmaku(seg, '20261018-231500', part_idx=0))


if __name__ == '__main__':
    unittest.main()
