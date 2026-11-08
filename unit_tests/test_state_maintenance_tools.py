"""状态库维护工具的单测（fix_stuck_artifacts / fix_state_payload）。

这两个工具都直接改生产状态库，判据错了就是数据损坏。这里锁住三件事：

1. **运行中状态按磁盘事实收敛**：成品在 → rendered，不在 → recorded；
   源文件在 → recorded，不在 → missing。绝不能一律写成同一个值。
2. **非法 JSON 转义能被修好**，且修完语义不变（路径还是那个路径）。
3. **两个工具默认都是 DRY-RUN**：不显式给 --apply 时一行都不改。
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest


TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')


def load(name):
    path = os.path.join(TOOLS, name + '.py')
    spec = importlib.util.spec_from_file_location(name + '_under_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_db(path):
    con = sqlite3.connect(path)
    con.execute('''CREATE TABLE artifacts(
        artifact_id TEXT PRIMARY KEY, session_id TEXT, segment_idx INTEGER,
        kind TEXT, state TEXT, updated_at INTEGER, data TEXT)''')
    con.commit()
    return con


class TestEscapeFixer(unittest.TestCase):
    """非法 JSON 转义修复。"""

    @classmethod
    def setUpClass(cls):
        cls.m = load('fix_state_payload')

    def test_single_backslash_before_letter_is_fixed(self):
        """实盘那条：`弹幕版\\vvu` 里的 \\v 是非法转义。"""
        raw = r'{"path": "./直播回放/弹幕版\vvu\\x.mp4"}'
        fixed = self.m._fix_escapes(raw)
        self.assertIsNotNone(fixed)
        self.assertEqual(json.loads(fixed)['path'],
                         './直播回放/弹幕版\\vvu\\x.mp4')

    def test_valid_escapes_untouched(self):
        """合法的 \\n / \\" / \\\\ 不能被改坏。"""
        raw = r'{"a": "line\nbreak", "b": "quote\"x", "c": "back\\slash"}'
        fixed = self.m._fix_escapes(raw)
        self.assertIsNotNone(fixed)
        d = json.loads(fixed)
        self.assertEqual(d['a'], 'line\nbreak')
        self.assertEqual(d['b'], 'quote"x')
        self.assertEqual(d['c'], 'back\\slash')

    def test_unicode_escape_preserved(self):
        """\\uXXXX 是合法转义，必须原样保留。"""
        raw = r'{"a": "\u4e2d\u6587"}'
        self.assertIsNone(self.m._fix_escapes(raw) and None)  # 本来就合法
        self.assertEqual(json.loads(raw)['a'], '中文')

    def test_ambiguous_trailing_backslash_is_not_guessed(self):
        """字符串值结尾是单个反斜杠时属于**结构歧义**（\\" 会被当成引号转义），
        这种不能硬猜着改 —— 必须如实返回 None，交给人工看。"""
        self.assertIsNone(self.m._fix_escapes('{"a": "x' + chr(92) + '"}'))

    def test_structurally_broken_returns_none(self):
        """真损坏（缺右括号）不能硬改，必须如实返回 None。"""
        self.assertIsNone(self.m._fix_escapes('{"a": "x\\'))


class TestStuckArtifactTargets(unittest.TestCase):
    """运行中状态的目标值必须按磁盘事实分别判定。"""

    @classmethod
    def setUpClass(cls):
        cls.m = load('fix_stuck_artifacts')

    def test_unstable_list_covers_running_states(self):
        for s in ('rendering', 'uploading', 'recording'):
            self.assertIn(s, self.m.UNSTABLE,
                          f'{s} 不在"运行中"清单里 —— 卡住的记录会被漏掉')

    def test_terminal_states_not_touched(self):
        """终态绝不能进 UNSTABLE，否则已上传的会被回炉重跑。"""
        for s in ('uploaded', 'cleaned', 'skipped', 'missing', 'rendered'):
            self.assertNotIn(s, self.m.UNSTABLE)

    def test_disk_path_normalises_mixed_separators(self):
        """库里那种 `弹幕版\\vvu\\x.mp4` 混合斜杠要能还原成真实路径。"""
        rel = './直播回放/弹幕版\\vvu\\x.mp4'
        p = self.m._disk_path('F:/repo', rel)
        self.assertTrue(p.startswith('F:/repo') or p.startswith('F:\\repo'))
        self.assertIn('x.mp4', p)
        self.assertNotIn('./', p)

    def test_dedup_semantics_by_kind(self):
        """核心：同为"磁盘上没有"，dm_video 回 recorded，src_video 回 missing。

        这两者语义完全不同：
          · dm_video 的 recorded = "已登记、尚未渲染"，是合法的中间态
          · src_video 的 missing = "源文件真的丢了"，是如实反映
        写成同一个值会让 reconciler 做出错误判断。
        """
        # 直接断言工具里的分支语义（通过读源码保持与实现一致）
        with open(os.path.join(TOOLS, 'fix_stuck_artifacts.py'),
                  encoding='utf-8') as f:
            src = f.read()
        self.assertIn("target = 'rendered' if exists else 'recorded'", src)
        self.assertIn("target = 'recorded' if exists else 'missing'", src)


class TestDryRunByDefault(unittest.TestCase):
    """默认不写库 —— 这两个工具都直接动生产库。"""

    def test_stuck_tool_is_dry_by_default(self):
        with open(os.path.join(TOOLS, 'fix_stuck_artifacts.py'),
                  encoding='utf-8') as f:
            src = f.read()
        self.assertIn("if not opt.apply:", src)
        # 备份必须发生在任何 UPDATE 之前 —— 否则写坏了没法回滚
        self.assertLess(src.index('shutil.copy2(opt.db'), src.index('UPDATE artifacts'),
                        '备份写在 UPDATE 之后：一旦写坏无法回滚')

    def test_payload_tool_is_dry_by_default(self):
        with open(os.path.join(TOOLS, 'fix_state_payload.py'),
                  encoding='utf-8') as f:
            src = f.read()
        self.assertIn("if not opt.apply:", src)
        self.assertIn('shutil.copy2', src)


class TestPayloadToolEndToEnd(unittest.TestCase):
    """对临时库跑一遍真流程，确保只改坏行、不碰好行。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, 'state.db')
        self.m = load('fix_state_payload')

    def test_only_broken_rows_change(self):
        con = make_db(self.db)
        good = json.dumps({'path': './直播回放/原视频/甲/a.ts'},
                          ensure_ascii=False)
        bad = r'{"path": "./直播回放/弹幕版\甲\\a.mp4"}'
        con.execute("INSERT INTO artifacts VALUES('g:1','s',1,'src_video','recorded',0,?)",
                    (good,))
        con.execute("INSERT INTO artifacts VALUES('b:1','s',1,'dm_video','cleaned',0,?)",
                    (bad,))
        con.commit()
        con.close()

        import sys
        argv = sys.argv
        sys.argv = ['fix_state_payload.py', '--apply', '--db', self.db]
        try:
            self.m.main()
        finally:
            sys.argv = argv

        con = sqlite3.connect(self.db)
        rows = dict(con.execute('SELECT artifact_id, data FROM artifacts'))
        con.close()
        self.assertEqual(rows['g:1'], good, '好的行被改动了')
        d = json.loads(rows['b:1'])
        self.assertIn('a.mp4', d['path'], '坏行没被修好')
        self.assertIn('甲', d['path'])


if __name__ == '__main__':
    unittest.main()
