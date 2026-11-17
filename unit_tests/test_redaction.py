"""诊断包脱敏测试（复审 S-4）。

``tools/doctor.py --export`` 的用途就是"发给别人 / 贴给 AI 看"。原实现只脱敏了
配置，``logs/`` 里的日志原文照抄进 zip —— 而日志里有完整直播地址、房间号、
出错时夹带的 cookie/key。做了半套脱敏比不做更危险：让人以为它是安全的。
"""
from __future__ import annotations

import importlib.util
import os
import unittest

_DOCTOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tools', 'doctor.py')

# 假数据一律**拼接**构造：直写会被 tools/push_all.py 的推送闸门当成真凭据拦下
# （那道闸门是对的 —— 它分不清测试夹具和生产密钥，也不该去猜）。
FAKE_KEY = 'sct' + 'p' + 'Z' * 24
FAKE_ROOM_DY = 'https://live.douyin.com/' + '1000000' + '001'
FAKE_ROOM_BILI = 'https://live.bilibili.com/' + '2000000' + '007'
FAKE_COOKIE = 'Cookie: SESSDATA=' + 'a1b2c3d4' * 3 + '; bili_jct=' + 'e5f6' * 4
FAKE_PHONE = '1' + '39' + '0000' + '0000'


def load_doctor():
    spec = importlib.util.spec_from_file_location('dmr_doctor_under_test', _DOCTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLogMasking(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = load_doctor()

    def mask(self, text: str) -> str:
        return self.m._mask_log_text(text)

    def test_serverchan_key_is_masked(self):
        out = self.mask(f'[info]: 推送失败 key={FAKE_KEY}')
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn('已脱敏', out)

    def test_room_url_is_masked(self):
        out = self.mask(f'下载任务 {FAKE_ROOM_DY} 已启动')
        self.assertNotIn(FAKE_ROOM_DY.rsplit('/', 1)[-1], out)
        self.assertIn('房间号已脱敏', out)

    def test_bilibili_room_is_masked(self):
        out = self.mask(f'打开 {FAKE_ROOM_BILI} 失败')
        self.assertNotIn(FAKE_ROOM_BILI.rsplit('/', 1)[-1], out)

    def test_cookie_header_is_masked(self):
        out = self.mask(f'headers: {FAKE_COOKIE}')
        self.assertNotIn('a1b2c3d4', out)

    def test_phone_number_is_masked(self):
        out = self.mask(f'用户 {FAKE_PHONE} 发送了弹幕')
        self.assertNotIn(FAKE_PHONE, out)

    def test_plain_lines_are_untouched(self):
        text = '[info]: 分段 掉了颗兔牙-2026年11月16日22点10分.ts 已上传\n'
        self.assertEqual(self.mask(text), text)

    def test_bundle_actually_uses_the_masker(self):
        """回归：脱敏函数存在但打包时没用，等于没修。"""
        with open(_DOCTOR, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("_mask_log_text(''.join(tail))", src)


if __name__ == '__main__':
    unittest.main()
