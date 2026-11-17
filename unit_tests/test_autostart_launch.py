"""tools/autostart_launch.py 的单元测试。

覆盖三类纯逻辑：
  · 命令行参数解析（--boot-fallback / --pretend-session / --dry-run）
  · logon_action 的四种判断（start / noop / migrate / skip）
  · dmr_processes 对 "pid,session" 输出的解析（含脏数据）

会话迁移是整个登录自启的核心，判断错一次就是"双开"或"窗口不见了"，
所以这里锁死。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tools', 'autostart_launch.py')


def load_module(argv=None):
    """按给定 argv 重新加载模块（模块级代码会解析参数）。"""
    old = sys.argv
    sys.argv = ['autostart_launch.py'] + (argv or [])
    try:
        spec = importlib.util.spec_from_file_location(
            'autostart_launch_under_test', MODULE_PATH)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.argv = old


class TestFlagParsing(unittest.TestCase):
    def test_defaults(self):
        m = load_module()
        self.assertEqual(m.BOOT_FALLBACK, 0)
        self.assertIsNone(m.PRETEND_SESSION)
        self.assertFalse(m.DRY_RUN)

    def test_boot_fallback_with_space(self):
        m = load_module(['--boot-fallback', '90'])
        self.assertEqual(m.BOOT_FALLBACK, 90)

    def test_boot_fallback_with_equals(self):
        m = load_module(['--boot-fallback=120'])
        self.assertEqual(m.BOOT_FALLBACK, 120)

    def test_boot_fallback_bad_value_uses_default(self):
        m = load_module(['--boot-fallback', 'abc'])
        self.assertEqual(m.BOOT_FALLBACK, 180)

    def test_boot_fallback_missing_value_uses_default(self):
        m = load_module(['--boot-fallback'])
        self.assertEqual(m.BOOT_FALLBACK, 180)

    def test_pretend_session(self):
        m = load_module(['--pretend-session', '3'])
        self.assertEqual(m.PRETEND_SESSION, 3)
        self.assertEqual(m.my_session(), 3)

    def test_dry_run(self):
        m = load_module(['--dry-run'])
        self.assertTrue(m.DRY_RUN)


class TestLogonAction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_no_process_means_start(self):
        self.assertEqual(self.m.logon_action([], 7), 'start')

    def test_same_session_is_noop(self):
        # venv 启动器 + C:\Python311 真身：同会话两个进程
        self.assertEqual(self.m.logon_action([(123, 7), (124, 7)], 7), 'noop')

    def test_other_session_migrates(self):
        self.assertEqual(self.m.logon_action([(123, 0)], 7), 'migrate')

    def test_mixed_sessions_migrates(self):
        self.assertEqual(self.m.logon_action([(123, 7), (124, 0)], 7),
                         'migrate')

    def test_foreign_but_we_are_session0_skips(self):
        # 本进程在 session 0：不能把用户会话里的实例杀掉
        self.assertEqual(self.m.logon_action([(123, 7)], 0), 'skip')

    def test_foreign_but_session_unknown_skips(self):
        self.assertEqual(self.m.logon_action([(123, 7)], -1), 'skip')


class TestDmrProcessesParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def _with_ps(self, output):
        # 2026-11-16 复审 N-8 后：dmr_processes 优先走 psutil（能看 exe/cwd/父进程），
        # psutil 不可用才退回命令行匹配。测试要验证的是"命令行输出怎么解析"，
        # 所以把 psutil 那一路显式关掉（返回 None = 探测不可用）。
        original = self.m.ps
        original_psutil = self.m._dmr_pids_psutil
        self.m.ps = lambda cmd: output
        self.m._dmr_pids_psutil = lambda: None
        try:
            return self.m.dmr_processes()
        finally:
            self.m.ps = original
            self.m._dmr_pids_psutil = original_psutil

    def test_parses_pid_session_pairs(self):
        self.assertEqual(self._with_ps('3456,7\n16296,7\n'),
                         [(3456, 7), (16296, 7)])

    def test_parses_crlf_and_single_line(self):
        self.assertEqual(self._with_ps('3456,7 16296,7'),
                         [(3456, 7), (16296, 7)])

    def test_empty_output(self):
        self.assertEqual(self._with_ps(''), [])

    def test_ignores_garbage_tokens(self):
        self.assertEqual(self._with_ps('notapid\n123,\nabc,7\n'), [])


class TestSuperviseDecisions(unittest.TestCase):
    """守护模式的判断矩阵（dry-run 只判断不动作，可安全调用）。"""

    def _run_dry(self, dmr_out, explorer_out):
        m = load_module(['--boot-fallback', '180', '--dry-run'])
        lines = []
        m.log = lines.append          # 别往生产 autostart.log 里写测试痕迹
        # 2026-11-16（审查报告 R-8）：进程查询语句改成了正则
        # `main\.py`，所以匹配键不能再写死 'main.py'（含未转义的点）。
        m.ps = lambda cmd: (dmr_out if 'main' in cmd else explorer_out)
        # 同 TestDmrProcessesParsing：关掉 psutil 那一路，只验证命令输出解析
        m._dmr_pids_psutil = lambda: None
        try:
            rc = m.mode_supervise()
        finally:
            m.ps = None
        return rc, lines

    def test_running_instance_is_left_alone(self):
        rc, lines = self._run_dry('3456,7', '1')
        self.assertEqual(rc, 0)
        self.assertIn('running', lines[-1])

    def test_dead_with_desktop_waits_for_logon_autostart(self):
        rc, lines = self._run_dry('', '2')  # 有桌面、无 DMR
        self.assertEqual(rc, 0)
        self.assertIn('wait-logon', lines[-1])

    def test_dead_without_desktop_waits_for_grace_first(self):
        rc, lines = self._run_dry('', '0')  # 无桌面、无 DMR
        self.assertEqual(rc, 0)
        self.assertIn('grace', lines[-1])


class TestOwnershipFilter(unittest.TestCase):
    """``_is_our_process``：只认本项目自己的 DMR（2026-11-16 复审 N-8）。

    生产实测：真身是 ``C:\\Python311\\python.exe main.py --quiet``，命令行里
    **没有项目名**；``.venv/Scripts/python.exe`` 只是启动器。旧判据（命令行含项目名）
    只认启动器，启动器一没就判"没在跑" → 守护反复拉起 → 崩溃循环。
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()
        cls.proj = os.path.normcase(os.path.abspath(cls.m.UP))

    @staticmethod
    def _proc(cmdline, exe='', cwd=None, parent=None):
        class P:
            def __init__(self):
                self.info = {'pid': 1, 'name': 'python.exe',
                             'cmdline': cmdline, 'exe': exe}

            def cwd(self):
                if cwd is None:
                    raise OSError('denied')
                return cwd

            def parent(self):
                return parent

            def exe(self):
                return exe
        return P()

    def test_venv_launcher_is_recognised(self):
        p = self._proc([os.path.join(self.m.UP, '.venv', 'Scripts', 'python.exe'),
                        'main.py'], exe=os.path.join(self.m.UP, '.venv', 'Scripts',
                                                    'python.exe'))
        self.assertTrue(self.m._is_our_process(p, self.proj))

    def test_real_interpreter_without_project_in_cmdline(self):
        """★核心：真身命令行没有项目名，靠 cwd / 父进程也要认出来。"""
        proc = self._proc([r'C:\Python311\python.exe', 'main.py', '--quiet'],
                          exe=r'C:\Python311\python.exe', cwd=self.m.UP)
        self.assertTrue(self.m._is_our_process(proc, self.proj))

    def test_child_of_launcher_is_recognised(self):
        """cwd 读不到（权限）时，父进程链仍能认出来。"""
        parent = self._proc([os.path.join(self.m.UP, '.venv', 'Scripts',
                                          'python.exe'), 'main.py'],
                            exe=os.path.join(self.m.UP, '.venv', 'Scripts',
                                             'python.exe'))
        proc = self._proc([r'C:\Python311\python.exe', 'main.py'],
                          exe=r'C:\Python311\python.exe', cwd=None, parent=parent)
        self.assertTrue(self.m._is_our_process(proc, self.proj))

    def test_other_project_is_rejected(self):
        proc = self._proc([r'C:\Python311\python.exe', 'main.py'],
                          exe=r'C:\Python311\python.exe', cwd=r'C:\OtherProject')
        self.assertFalse(self.m._is_our_process(proc, self.proj))


class TestSuperviseLock(unittest.TestCase):
    """复审 R-7：守护模式必须持锁，否则两个守护会各拉一个 DMR。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dmr-supervise-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.m = load_module(['--boot-fallback', '180', '--dry-run'])
        self.m.log = lambda *_a, **_k: None

    def _lock(self):
        return os.path.join(self.tmp, 'supervise.lock')

    def test_second_acquire_is_refused(self):
        self.assertTrue(self.m.acquire_lock(path=self._lock()))
        # 模拟"另一个进程"：本进程放弃所有权，但锁文件仍在
        self.m._LOCK_HELD = None
        self.assertFalse(self.m.acquire_lock(path=self._lock()),
                         '锁文件在场且持有者pid存活时必须拒绝')

    def test_release_allows_reacquire(self):
        self.assertTrue(self.m.acquire_lock(path=self._lock()))
        self.m.release_lock()
        self.assertFalse(os.path.exists(self._lock()))
        self.assertTrue(self.m.acquire_lock(path=self._lock()))
        self.m.release_lock()

    def test_dead_pid_lock_is_reclaimed_without_waiting_max_age(self):
        """崩溃留下的锁：pid 不在了就立刻接管，不能干等 30 分钟。"""
        try:
            import psutil  # noqa: F401
        except ImportError:
            self.skipTest('需要 psutil 判 pid 存活')
        with open(self._lock(), 'w', encoding='utf-8') as f:
            f.write('999999 0')          # 几乎不可能存在的 pid
        self.assertTrue(self.m.acquire_lock(path=self._lock()))
        self.m.release_lock()

    def test_supervise_returns_immediately_when_lock_held(self):
        """真身（非 dry-run）发现锁已被活着的守护持有 → 直接退出，不进循环。"""
        m = load_module(['--boot-fallback', '180'])
        m.log = lambda *_a, **_k: None
        lock = self._lock()
        with open(lock, 'w', encoding='utf-8') as f:
            f.write(f'{os.getpid()} 0')
        m.SUPERVISE_LOCK = lock
        self.assertEqual(m.mode_supervise(), 0)


if __name__ == '__main__':
    unittest.main()
