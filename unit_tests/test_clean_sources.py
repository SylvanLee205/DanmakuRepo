"""tools/clean_sources.py 的判据测试。

这个工具是"删源视频"的，判据错了就是**不可恢复的数据丢失**（源视频删了
再也渲染不出弹幕版）。2026-11-09 体检时发现它有两条致命缺陷：

1. **布局没跟上 11-02 的目录改版**
   它以为 ``直播回放/<主播>/`` 是原视频、``直播回放/<主播>（弹幕版）/`` 是成品。
   改版后 ``直播回放/`` 下只有 ``原视频`` 和 ``弹幕版`` 两个容器，老逻辑把
   这两个当主播名去找 ``原视频（弹幕版）``，永远找不到 → 工具静默退化成
   "没有可清理的文件"，4 个已上传分段的源视频（2.2GB）躺了一周。

2. **云端校验用截断前缀匹配（会误判！）**
   ``n.startswith(stem[:len(stem) - 6])`` 把 ``14点19分`` 削成 ``...11月07日``，
   于是当天的任意一场录制都会命中。实盘险情：
     vvu-...11月07日14点19分.ts   → 误命中 ...11月07日15点19分（弹幕版）
     君君.-...11月06日18点52分.ts → 误命中 ...11月06日18点03分（弹幕版）
   而这两段恰恰**渲染失败、从未上传**。修之前它们被列为"可安全清理"，
   一条 --apply 就永久丢失。

3. **完全无视 24h 延迟清理队列**（最严重）
   工具的判据是"本地已渲染就能删"，但正式流程是渲染→上传→等 24h→才删。
   体检时 34 条队列全是未到期的，工具却把其中 66 个文件（40.9GB）列成可删。
"""
import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tools', 'clean_sources.py')


def load_module():
    spec = importlib.util.spec_from_file_location(
        'clean_sources_under_test', MODULE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestCloudMatch(unittest.TestCase):
    """云端同名判定必须严格，不许靠截断前缀蒙对。"""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_exact_name_hits(self):
        cloud = {'vvu-2026年11月07日14点19分（弹幕版）.mp4'}
        self.assertTrue(
            self.m._exists_in_cloud('vvu-2026年11月07日14点19分', cloud))

    def test_same_day_different_time_does_not_hit(self):
        """★核心回归：同一天另一场录制不能被当成"已有"。"""
        cloud = {
            'vvu-2026年11月07日15点19分（弹幕版）.mp4',
            'vvu-2026年11月07日16点19分（弹幕版）.mp4',
        }
        self.assertFalse(
            self.m._exists_in_cloud('vvu-2026年11月07日14点19分', cloud),
            '同日不同时段被误判为云端已有 —— 会导致删掉从未上传的源视频')

    def test_different_day_does_not_hit(self):
        cloud = {'君君.-2026年11月06日18点03分（弹幕版）.mp4'}
        self.assertFalse(
            self.m._exists_in_cloud('君君.-2026年11月06日18点52分', cloud))

    def test_renamed_variant_hits(self):
        """DMR 对重名产物会加 (2)，这种是同一内容，应当命中。"""
        cloud = {'掉了颗兔牙-2026年11月03日00点20分(2)（弹幕版）.mp4'}
        self.assertTrue(
            self.m._exists_in_cloud('掉了颗兔牙-2026年11月03日00点20分', cloud))

    def test_empty_cloud_never_hits(self):
        self.assertFalse(self.m._exists_in_cloud('anything', set()))

    def test_source_code_has_no_truncated_prefix_match(self):
        """静态断言：不许再出现 stem[:-6] 那种截断前缀比对。"""
        with open(MODULE_PATH, encoding='utf-8') as fh:
            src = fh.read()
        self.assertNotRegex(
            src, r'stem\[:\s*max\(\s*1\s*,\s*len\(stem\)\s*-\s*\d+\s*\)\]',
            '云端校验又用回了截断前缀匹配（会把同日任意分段误判为已上传）')


class TestRenderedMatch(unittest.TestCase):
    """本地成品判定要容忍 (n) 重名后缀。"""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_plain_match(self):
        self.assertTrue(self.m._exists_rendered(
            'a-2026年11月02日14点40分',
            {'a-2026年11月02日14点40分（弹幕版）.mp4'}))

    def test_duplicate_suffix_match(self):
        """★ 掉了颗兔牙 那个 755MB 就是栽在这里。"""
        self.assertTrue(self.m._exists_rendered(
            '掉了颗兔牙-2026年11月03日00点20分(2)',
            {'掉了颗兔牙-2026年11月03日00点20分(2)（弹幕版）.mp4'}))

    def test_missing_render(self):
        self.assertFalse(self.m._exists_rendered(
            'a-2026年11月02日14点40分',
            {'a-2026年11月02日15点40分（弹幕版）.mp4'}))


class TestLayoutScan(unittest.TestCase):
    """新旧两种目录布局都要能扫到。"""

    def setUp(self):
        self.m = load_module()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._orig = self.m.REPLAY
        self.m.REPLAY = self.tmp

    def tearDown(self):
        self.m.REPLAY = self._orig

    def _mkdir(self, *parts):
        d = os.path.join(self.tmp, *parts)
        os.makedirs(d, exist_ok=True)
        return d

    def test_new_layout_is_scanned(self):
        """新布局：原视频/<主播>/ + 弹幕版/<主播>/"""
        self._mkdir('原视频', '甲')
        self._mkdir('弹幕版', '甲')
        pairs = self.m._scan_layouts()
        tasks = {t for _, _, t in pairs}
        self.assertIn('甲', tasks, '新布局没被扫到 —— 工具会静默变成"无文件可清"')

    def test_old_layout_is_scanned(self):
        """老布局：<主播>/ + <主播>（弹幕版）/"""
        self._mkdir('乙')
        self._mkdir('乙（弹幕版）')
        pairs = self.m._scan_layouts()
        tasks = {t for _, _, t in pairs}
        self.assertIn('乙', tasks)

    def test_container_dirs_are_not_tasks(self):
        """原视频/弹幕版 是容器，不能被当成主播名。"""
        self._mkdir('原视频', '丙')
        self._mkdir('弹幕版', '丙')
        pairs = self.m._scan_layouts()
        tasks = {t for _, _, t in pairs}
        self.assertNotIn('原视频', tasks)
        self.assertNotIn('弹幕版', tasks)


class TestPendingCleanGuard(unittest.TestCase):
    """24h 延迟清理队列里的文件绝不能被这个工具删。"""

    def setUp(self):
        self.m = load_module()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._orig_root, self._orig_replay = self.m.ROOT, self.m.REPLAY
        self.m.ROOT = self.tmp
        self.m.REPLAY = os.path.join(self.tmp, '直播回放')
        os.makedirs(os.path.join(self.tmp, 'logs'), exist_ok=True)

    def tearDown(self):
        self.m.ROOT, self.m.REPLAY = self._orig_root, self._orig_replay

    def _write_queue(self, entries):
        with open(os.path.join(self.tmp, 'logs', 'pending_clean.json'),
                  'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False)

    def _make_ripe_file(self, task, name, *, render=True):
        """造一个"老文件 + 有成品"的理想删除候选。"""
        src = os.path.join(self.m.REPLAY, '原视频', task)
        out = os.path.join(self.m.REPLAY, '弹幕版', task)
        os.makedirs(src, exist_ok=True)
        os.makedirs(out, exist_ok=True)
        p = os.path.join(src, name)
        with open(p, 'w') as f:
            f.write('x')
        old = 100000
        os.utime(p, (old, old))
        if render:
            rp = os.path.join(out, os.path.splitext(name)[0] + '（弹幕版）.mp4')
            with open(rp, 'w') as f:
                f.write('y')
        return p

    def test_pending_file_is_protected(self):
        """★核心回归：在队列里的文件必须跳过（哪怕它"已渲染"）。"""
        name = '甲-2026年11月08日23点00分.ts'
        p = self._make_ripe_file('甲', name)
        self._write_queue([{
            'id': 'x', 'due_ts': 9999999999, 'created_ts': 0, 'delay': 86400,
            'pairs': [[f'./直播回放/原视频/甲\\{name}',
                       f'./直播回放/原视频/甲\\{name[:-3]}.ass']],
            'source': 'replay/甲',
        }])

        ok, skipped = self.m.find_candidates(min_age_minutes=30)

        self.assertNotIn(p, [x[0] for x in ok],
                         '在 24h 延迟队列里的文件被列为可清理 —— 会绕过保护窗口')
        self.assertIn(p, [x[0] for x in skipped])

    def test_unlisted_file_still_cleanable(self):
        """不在队列里的僵尸文件仍然可以清（不能矫枉过正）。"""
        name = '乙-2026年11月02日14点40分.ts'
        p = self._make_ripe_file('乙', name)
        self._write_queue([])

        ok, _ = self.m.find_candidates(min_age_minutes=30)
        self.assertIn(p, [x[0] for x in ok])

    def test_broken_queue_protects_everything(self):
        """队列读不出来时按"全部保护"处理 —— 宁可不清，不可错清。"""
        name = '丙-2026年11月02日14点40分.ts'
        p = self._make_ripe_file('丙', name)
        with open(os.path.join(self.tmp, 'logs', 'pending_clean.json'),
                  'w', encoding='utf-8') as f:
            f.write('{ 这不是合法 JSON')

        ok, skipped = self.m.find_candidates(min_age_minutes=30)
        self.assertNotIn(p, [x[0] for x in ok])

    def test_file_without_render_is_protected(self):
        """没渲染产物的源文件永远不能删（这是最基础的防护）。"""
        name = '丁-2026年11月07日14点19分.ts'
        p = self._make_ripe_file('丁', name, render=False)
        self._write_queue([])

        ok, skipped = self.m.find_candidates(min_age_minutes=30)
        self.assertNotIn(p, [x[0] for x in ok])


if __name__ == '__main__':
    unittest.main()
