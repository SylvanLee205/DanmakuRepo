"""``WebApi`` 的访问收窄测试（复审 S-1）。

背景：``force_login=False`` 时 ``login_required`` 直接放行，配合
``host: 0.0.0.0`` 等于"局域网内任何人可以停止录制、删任务、改配置"。
免登录在单机上确实是合理需求，但"免登录 + 对外监听"必须是配置里的显式选择。
"""
from __future__ import annotations

import queue
import unittest

from DMR.WebService.webapi import WebApi


def _pipe():
    return (queue.Queue(), queue.Queue())


class TestNoAuthBinding(unittest.TestCase):

    def make(self, **kwargs) -> WebApi:
        return WebApi(_pipe(), **kwargs)

    def test_no_login_remote_binding_is_narrowed(self):
        api = self.make(host='0.0.0.0', force_login=False,
                        username='admin', password='S3cure-pass')
        self.assertEqual(api.host, '127.0.0.1',
                         '免登录时不得监听 0.0.0.0')

    def test_no_login_local_binding_is_kept(self):
        api = self.make(host='127.0.0.1', force_login=False,
                        username='admin', password='S3cure-pass')
        self.assertEqual(api.host, '127.0.0.1')

    def test_explicit_override_keeps_remote(self):
        """确实要局域网免登录：显式开 allow_insecure_remote 才放行。"""
        api = self.make(host='0.0.0.0', force_login=False,
                        username='admin', password='S3cure-pass',
                        allow_insecure_remote=True)
        self.assertEqual(api.host, '0.0.0.0')

    def test_remote_binding_with_login_and_strong_password_is_kept(self):
        """登录开启 + 强口令 → 原有行为不变（不误伤局域网使用）。"""
        api = self.make(host='0.0.0.0', force_login=True,
                        username='admin', password='S3cure-pass')
        self.assertEqual(api.host, '0.0.0.0')

    def test_default_password_still_narrowed(self):
        """默认口令的老防线不能被本次改动碰坏。"""
        api = self.make(host='0.0.0.0', force_login=True,
                        username='admin', password='admin')
        self.assertEqual(api.host, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
