"""tools/add_room.py 的单元测试。

重点锁死两件事：

1. **ctypes 句柄不能被截断**（2026-11-06 实盘崩溃的根因）
   双击「添加主播」报退出码 -1073741819（= 0xC0000005 访问冲突），
   控制台一行输出都没有。真因不是"剪贴板被占用"，而是：
   `GetClipboardData` 没声明 `restype` → ctypes 按 `c_int`（32 位）处理 →
   64 位 HANDLE 高位被静默截断 → 把残指针传给 `GlobalLock` → 段错误。
   C 层崩溃，Python 连 traceback 都没机会打。

   这条断言检查的是"所有吃/返回句柄的 API 都显式声明了指针宽度的类型"，
   属于静态检查 —— 不依赖真的去读剪贴板（CI 里没有剪贴板也能跑）。

2. **链接解析**：抖音分享文本 / 短链 / 纯房间号都要能认出来。

为什么放在单测里：这个 bug 极难复现（取决于那一刻句柄的高位是不是 0），
但一旦命中用户看到的就是"双击后什么都没发生"。锁死写法比锁死行为更可靠。
"""
import importlib.util
import os
import re
import unittest

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tools', 'add_room.py')


def load_module():
    spec = importlib.util.spec_from_file_location(
        'add_room_under_test', MODULE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestClipboardHandleWidth(unittest.TestCase):
    """句柄类 API 必须显式声明 64 位指针类型。

    ctypes 的默认 restype 是 ``c_int``（32 位）。在 64 位 Python 下，
    任何返回 HANDLE / HGLOBAL / 指针的 Win32 API，不声明 restype 就会被
    截断 —— 而 Windows 的堆句柄高位经常非零，截断后必定崩溃。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = open(MODULE_PATH, encoding='utf-8').read()

    def test_getclipboarddata_declares_void_p(self):
        """GetClipboardData 必须声明 restype = c_void_p（不能靠默认 c_int）。"""
        self.assertRegex(
            self.src,
            r'GetClipboardData\.restype\s*=\s*ctypes\.c_void_p',
            'GetClipboardData 未声明 restype=c_void_p：'
            '64 位下句柄会被截断成 32 位，进而段错误（0xC0000005）')

    def test_globallock_declares_void_p(self):
        """GlobalLock 返回指针，必须声明 c_void_p。"""
        self.assertRegex(
            self.src,
            r'GlobalLock\.restype\s*=\s*ctypes\.c_void_p',
            'GlobalLock 未声明 restype=c_void_p：返回指针会被截断')

    def test_no_c_wchar_p_as_restype(self):
        """不要用 c_wchar_p 作 restype。

        c_wchar_p 会让 ctypes **自己解引用**返回值；指针一旦无效，
        崩溃发生在 ctypes 内部，同样没有 traceback。
        正确做法：拿 c_void_p 原始指针，再 ctypes.wstring_at 手动读。
        """
        self.assertNotRegex(
            self.src,
            r'\.restype\s*=\s*ctypes\.c_wchar_p',
            'restype=c_wchar_p 会让 ctypes 自行解引用，无效指针时直接崩；'
            '应改用 c_void_p + ctypes.wstring_at')

    def test_uses_wstring_at(self):
        """读字符串走 wstring_at（配合 c_void_p 句柄）。"""
        self.assertIn('wstring_at', self.src,
                      '应从 c_void_p 原始指针用 ctypes.wstring_at 手动读字符串')

    def test_unlock_called_on_handle(self):
        """GlobalUnlock 必须收到同一个原始句柄。"""
        self.assertRegex(self.src, r'GlobalUnlock\s*\(\s*handle\s*\)',
                         'GlobalUnlock 应传原始 handle')


class TestRoomIdParsing(unittest.TestCase):
    """直播间链接 / 房间号解析。"""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_plain_room_id(self):
        self.assertEqual(self.m.parse_room_ids('865102911165'), ['865102911165'])

    def test_full_url(self):
        ids = self.m.parse_room_ids('https://live.douyin.com/865102911165')
        self.assertEqual(ids, ['865102911165'])

    def test_share_text_with_noise(self):
        """抖音分享出来是一坨带表情和说明的文字，要能把链接抠出来。"""
        text = ('7- #在抖音，记录美好生活#【某某】正在直播，来和我一起支持Ta吧。'
                '复制下方链接，打开【抖音】，直接观看直播！ '
                'https://live.douyin.com/865102911165')
        self.assertEqual(self.m.parse_room_ids(text), ['865102911165'])

    def test_multiple_links(self):
        text = ('https://live.douyin.com/111111111 '
                'https://live.douyin.com/222222222')
        ids = self.m.parse_room_ids(text)
        self.assertEqual(len(ids), 2)
        self.assertIn('111111111', ids)
        self.assertIn('222222222', ids)

    def test_empty_returns_empty(self):
        self.assertEqual(self.m.parse_room_ids(''), [])
        self.assertEqual(self.m.parse_room_ids('   '), [])

    def test_dedup_same_id(self):
        """同一个房间号出现多次只算一个。"""
        ids = self.m.parse_room_ids('111111111 111111111')
        self.assertEqual(ids, ['111111111'])


class TestConfigTemplate(unittest.TestCase):
    """生成的配置必须符合项目约定（否则 DMR 加载会出问题）。"""

    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_template_has_four_switches(self):
        for key in ('auto_render', 'auto_upload', 'auto_clean'):
            self.assertIn(key, self.m.TEMPLATE, '模板缺少开关 %s' % key)

    def test_template_dltype_live(self):
        self.assertIn("dltype: live", self.m.TEMPLATE)

    def test_template_output_dir_uses_source_root(self):
        """输出目录必须是新布局的 原视频/ 下，不能是老的平铺路径。"""
        self.assertIn('直播回放/原视频/', self.m.TEMPLATE,
                      '模板的 output_dir 应指向 直播回放/原视频/<主播>（新布局）')

    def test_bad_chars_cover_windows_illegal(self):
        """Windows 文件名非法字符必须覆盖全。"""
        for ch in '\\/:*?"<>|':
            self.assertIn(ch, self.m.BAD_CHARS, 'BAD_CHARS 漏了 %r' % ch)


if __name__ == '__main__':
    unittest.main()
