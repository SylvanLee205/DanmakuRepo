"""录制目录新布局（2026-11-02）的契约测试。

布局：
    直播回放/原视频/<主播>/    源视频 + .ass
    直播回放/弹幕版/<主播>/    渲染后的成品

为什么值得单测：这几个函数是**路径计算的唯一来源**，而路径错了的后果
是实打实的数据问题 —— 成品放到源目录里会被"删源视频"逻辑误伤，
放错位置则磁盘水位保护永远找不到成品、永远不敢删源。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DMR.paths import (  # noqa: E402
    DANMAKU_SUBDIR, DM_MARK, SOURCE_SUBDIR,
    danmaku_dir_for, danmaku_root, is_danmaku_path,
    recordings_root, source_root, streamer_of,
)


class TestLayout(unittest.TestCase):
    #: 注意 os.path.join('F:', 'x') 在 Windows 上是 'F:x'（盘符相对路径），
    #: 所以这里统一用带分隔符的盘符根。
    BASE = os.path.join('F:\\', 'DanmakuRepo')

    def test_new_layout_source_to_danmaku(self):
        src = os.path.join(self.BASE, '直播回放', SOURCE_SUBDIR,
                           '小鱼大王', '小鱼大王-2026年11月02日17点57分.ts')
        self.assertEqual(
            danmaku_dir_for(src),
            os.path.join(self.BASE, '直播回放', DANMAKU_SUBDIR, '小鱼大王'))

    def test_old_layout_also_lands_in_new_place(self):
        """没迁 output_dir 的老任务，成品也必须落进 弹幕版/ 而不是原地打转。"""
        src = os.path.join(self.BASE, '直播回放', 'vvu',
                           'vvu-2026年11月02日14点40分.ts')
        self.assertEqual(
            danmaku_dir_for(src),
            os.path.join(self.BASE, '直播回放', DANMAKU_SUBDIR, 'vvu'))

    def test_accepts_dir_as_well_as_file(self):
        d = os.path.join('直播回放', SOURCE_SUBDIR, 'vvu')
        self.assertTrue(danmaku_dir_for(d).endswith(os.path.join(DANMAKU_SUBDIR, 'vvu')))

    def test_streamer_of_strips_mark(self):
        self.assertEqual(streamer_of(os.path.join('直播回放', 'vvu（弹幕版）')), 'vvu')
        self.assertEqual(streamer_of(os.path.join('直播回放', SOURCE_SUBDIR, 'vvu')), 'vvu')
        self.assertEqual(streamer_of(''), '')

    def test_danmaku_dir_never_nests_twice(self):
        """幂等：把成品目录再喂进去，不能变成 弹幕版/弹幕版/..."""
        first = danmaku_dir_for(os.path.join('直播回放', SOURCE_SUBDIR, 'vvu', 'a.ts'))
        second = danmaku_dir_for(os.path.join(first, 'a（弹幕版）.mp4'))
        self.assertNotIn(
            os.path.join(DANMAKU_SUBDIR, DANMAKU_SUBDIR),
            os.path.normpath(second))

    def test_is_danmaku_path(self):
        self.assertTrue(
            is_danmaku_path(os.path.join('直播回放', DANMAKU_SUBDIR, 'vvu',
                                         'vvu-某场' + DM_MARK + '.mp4')))
        self.assertFalse(
            is_danmaku_path(os.path.join('直播回放', SOURCE_SUBDIR, 'vvu',
                                         'vvu-某场.ts')))
        self.assertFalse(is_danmaku_path(''))
        self.assertFalse(is_danmaku_path(None))

    def test_roots_under_data_root(self):
        self.assertTrue(recordings_root('X:').endswith('直播回放'))
        self.assertEqual(danmaku_root('X:'),
                         os.path.join('X:', '直播回放', DANMAKU_SUBDIR))
        # 本机还没迁移完时 source_root 退回录制根，不能是别的目录
        self.assertTrue(source_root('X:').startswith(os.path.join('X:', '直播回放')))

    def test_mark_constant_unchanged(self):
        """文件名后缀是"弹幕版永不删除"硬保护的判据，谁改谁出事。"""
        self.assertEqual(DM_MARK, '（弹幕版）')


if __name__ == '__main__':
    unittest.main()
