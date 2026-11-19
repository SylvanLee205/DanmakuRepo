"""失败上传清单的自净：同一个文件后来说传成功了，旧失败记录必须作废。

2026-11-19 重启实测：目录名 emoji 修好之后，状态库对账把 14 条失败上传全部
补传成功（云端已逐个核对到文件），但 ``.temp/failed_uploads.json`` 里的老记录
没人删 —— 每次启动都打印「Loaded 14 failed upload tasks」，WebUI 里还挂着
"待重试"，点一下就把已经在云端的 2GB+ 重传一遍。

⚠️ 清理时机不能放在启动："按状态库 UPLOADED 判断"的写法实测一条都清不掉 ——
状态库只保留未完结的场次，一场直播的产物全部进终态后，下次启动对账会把整场
删掉（D4「清理场次 N 个」），证据随之消失。只有"上传成功那一刻按路径对账"可靠。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import unittest

from DMR.Uploader import Uploader
from DMR.utils import VideoInfo


class _Base(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix='dmr-failed-list-')
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        # 不用构造函数：那会立刻去读生产 .temp/failed_uploads.json
        self.up = Uploader.__new__(Uploader)
        self.up.logger = logging.getLogger('unit-test')
        self.up.failed_tasks = {}
        self.up.failed_tasks_file = os.path.join(self.tmpdir,
                                                 'failed_uploads.json')

    def add_task(self, uid: str, *paths: str) -> None:
        self.up.failed_tasks[uid] = {
            'uuid': uid,
            'taskname': '主播',
            'source': 'replay/主播',
            'engine': 'subprocess',
            'files': [VideoInfo(path=p) for p in paths],
        }

    def succeed(self, *paths: str) -> None:
        self.up._forget_failed_for_files([VideoInfo(path=p) for p in paths])

    def on_disk(self) -> dict:
        with open(self.up.failed_tasks_file, encoding='utf-8') as f:
            return json.load(f)


class TestForgetFailedForFiles(_Base):

    def test_success_drops_the_stale_record(self):
        path = r'直播回放\弹幕版\主播\主播-2026年11月18日17点24分（弹幕版）.mp4'
        self.add_task('u1', path)

        self.succeed(path)

        self.assertEqual(self.up.failed_tasks, {})
        self.assertEqual(self.on_disk(), {}, '清单文件也必须同步清掉')

    def test_other_file_keeps_the_record(self):
        failed = r'直播回放\弹幕版\主播\主播-2026年11月18日18点24分（弹幕版）.mp4'
        other = r'直播回放\弹幕版\主播\主播-2026年11月18日19点24分（弹幕版）.mp4'
        self.add_task('u2', failed)

        self.succeed(other)

        self.assertIn('u2', self.up.failed_tasks, '别的文件成功不能连带删')

    def test_multi_file_task_needs_all_files(self):
        """一个任务带多个文件时，只成功一个不能算这条记录已了结。"""
        done = r'直播回放\弹幕版\主播\a.mp4'
        todo = r'直播回放\弹幕版\主播\b.mp4'
        self.add_task('u3', done, todo)

        self.succeed(done)

        self.assertIn('u3', self.up.failed_tasks)

    def test_multi_file_task_dropped_when_all_succeed(self):
        one = r'直播回放\弹幕版\主播\c.mp4'
        two = r'直播回放\弹幕版\主播\d.mp4'
        self.add_task('u4', one, two)

        self.succeed(one, two)

        self.assertEqual(self.up.failed_tasks, {})

    def test_path_normalisation_matches(self):
        """任务里的 '\' 分隔相对路径与带 './' 的写法是同一个文件。"""
        self.add_task('u5', r'直播回放\弹幕版\主播\主播-2026年11月18日21点24分（弹幕版）.mp4')

        self.succeed('./直播回放/弹幕版/主播/主播-2026年11月18日21点24分（弹幕版）.mp4')

        self.assertEqual(self.up.failed_tasks, {})

    def test_only_the_matching_record_is_removed(self):
        hit = r'直播回放\弹幕版\主播\e.mp4'
        miss = r'直播回放\弹幕版\主播\f.mp4'
        self.add_task('u6', hit)
        self.add_task('u7', miss)

        self.succeed(hit)

        self.assertNotIn('u6', self.up.failed_tasks)
        self.assertIn('u7', self.up.failed_tasks)

    def test_empty_input_is_noop(self):
        self.add_task('u8', r'直播回放\弹幕版\主播\g.mp4')

        self.up._forget_failed_for_files([])
        self.up._forget_failed_for_files([VideoInfo(path=None)])

        self.assertIn('u8', self.up.failed_tasks)


if __name__ == '__main__':
    unittest.main()
