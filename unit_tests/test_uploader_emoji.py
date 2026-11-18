"""远程路径的 emoji 清洗（百度网盘 API 拒绝非 BMP 字符）。

2026-11-18 生产事故：``🐈小鱼`` 三段弹幕版上传全部失败，rclone 报
``Update mkParentDir failed: 500 Internal Server Error``。原因是清洗只做了
文件名，**目录名里的 emoji 没管** —— 文件名换成 ``小鱼-….mp4`` 了，父目录还是
``🐈小鱼``，于是每次都在 mkdir 上死掉，重试 3 轮后放弃，成品永久滞留本地
（既不会补传，也不会被 24h 清理碰到）。

实测（rclone 1.75.1 + cd2 WebDAV）：
    rclone mkdir "cd2:百度网盘/DMR录播/.emoji-test-🐈"  → 3 次重试全 500
"""
from __future__ import annotations

import unittest

from DMR.Uploader.subprocess_uploader import (
    SubprocessUploader,
    has_bad_remote_chars,
    safe_remote_name,
)

CAT = '🐈'


class TestSafeRemoteName(unittest.TestCase):

    def test_emoji_is_stripped(self):
        self.assertEqual(safe_remote_name(f'{CAT}小鱼-2026年11月17日20点23分（弹幕版）.mp4'),
                         '小鱼-2026年11月17日20点23分（弹幕版）.mp4')

    def test_plain_name_is_unchanged(self):
        self.assertEqual(safe_remote_name('小鱼-2026年11月17日20点23分.mp4'),
                         '小鱼-2026年11月17日20点23分.mp4')

    def test_bad_chars_detected(self):
        self.assertTrue(has_bad_remote_chars(f'{CAT}小鱼'))
        self.assertFalse(has_bad_remote_chars('小鱼'))


class TestRemotePathSanitize(unittest.TestCase):

    def setUp(self):
        self.up = SubprocessUploader()

    def test_dir_component_is_cleaned(self):
        new, changed = self.up._sanitize_remote_path(f'cd2:百度网盘/DMR录播/{CAT}小鱼')
        self.assertEqual(new, 'cd2:百度网盘/DMR录播/小鱼')
        self.assertEqual(changed, [(f'{CAT}小鱼', '小鱼')])

    def test_remote_prefix_is_preserved(self):
        new, _ = self.up._sanitize_remote_path(f'cd2:百度网盘/{CAT}x')
        self.assertTrue(new.startswith('cd2:'))

    def test_clean_path_is_untouched(self):
        path = 'cd2:百度网盘/DMR录播/小鱼'
        new, changed = self.up._sanitize_remote_path(path)
        self.assertEqual(new, path)
        self.assertEqual(changed, [])


class TestSanitizeRcloneCommand(unittest.TestCase):

    def setUp(self):
        self.up = SubprocessUploader()

    def _cmd(self, *args):
        return ['rclone'] + list(args)

    def test_copy_with_emoji_filename_becomes_copyto(self):
        out = self.up._sanitize_rclone_copy(
            self._cmd('copy', f'a/{CAT}小鱼-1.mp4', 'cd2:盘/录播/小鱼'), None)
        self.assertEqual(out[:2], ['rclone', 'copyto'])
        self.assertEqual(out[3], 'cd2:盘/录播/小鱼/小鱼-1.mp4')

    def test_emoji_dir_forces_target_rewrite(self):
        """文件名干净、目录带 emoji：必须换目标路径，否则 mkdir 必 500。"""
        out = self.up._sanitize_rclone_copy(
            self._cmd('copy', 'a/小鱼-1.mp4', f'cd2:盘/录播/{CAT}小鱼'), None)
        self.assertEqual(out[1], 'copy')
        self.assertEqual(out[3], 'cd2:盘/录播/小鱼')

    def test_copyto_target_is_cleaned(self):
        out = self.up._sanitize_rclone_copy(
            self._cmd('copyto', 'a/x.mp4', f'cd2:盘/录播/{CAT}小鱼/x.mp4'), None)
        self.assertEqual(out[1], 'copyto')
        self.assertEqual(out[3], 'cd2:盘/录播/小鱼/x.mp4')

    def test_both_file_and_dir_are_cleaned(self):
        out = self.up._sanitize_rclone_copy(
            self._cmd('copy', f'a/{CAT}小鱼-1.mp4', f'cd2:盘/录播/{CAT}小鱼'), None)
        self.assertEqual(out[1], 'copyto')
        self.assertEqual(out[3], 'cd2:盘/录播/小鱼/小鱼-1.mp4')

    def test_clean_command_is_returned_as_is(self):
        cmd = self._cmd('copy', 'a/x.mp4', 'cd2:盘/录播/小鱼', '--retries', '5')
        self.assertEqual(self.up._sanitize_rclone_copy(cmd, None), cmd)

    def test_non_rclone_command_is_untouched(self):
        cmd = ['python', 'upload.py', 'a/x.mp4', f'cd2:盘/{CAT}']
        self.assertEqual(self.up._sanitize_rclone_copy(cmd, None), cmd)

    def test_extra_flags_are_preserved(self):
        out = self.up._sanitize_rclone_copy(
            self._cmd('copy', 'a/x.mp4', f'cd2:盘/{CAT}小鱼', '--retries', '5'), None)
        self.assertEqual(out[-2:], ['--retries', '5'])


if __name__ == '__main__':
    unittest.main()
