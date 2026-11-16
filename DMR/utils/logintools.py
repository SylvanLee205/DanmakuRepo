import subprocess
import os
import re
from .toolsmgr import ToolsList
from .runcmd import run_and_capture_output

__all__ = ['bili_islogin', 'bili_login']

def bili_islogin(cookies_path:str, **kwargs) -> bool:
    renew_args = [ToolsList.get('biliup'), '-u', cookies_path, 'renew']
    # 用临时文件收输出：原来的 PIPE + wait() + read() 在输出超过管道缓冲时会死锁，
    # 而 bufsize=10**8 既解决不了死锁、又白白预留上百 MB 内存。
    out, returncode = run_and_capture_output(renew_args, timeout=60)
    # 判据：退出码优先；"error" 用行首匹配，避免 "no error"/"error_count: 0"
    # 这类正常输出被判成登录失败（原实现是 'error' in out.lower()）。
    if returncode != 0:
        return False
    return re.search(r'(?im)^\s*(?:error|failed)\b', out or '') is None

def bili_login(cookies:str, account:str=None, **kwargs):
    if not (cookies or account):
        raise ValueError('cookies or account must be set.')
    if cookies is None:
        account = account
        cookies = f'.login_info/{account}.json'
    else:
        account = os.path.basename(cookies).split('.')[0]
        cookies = cookies
    
    os.makedirs(os.path.dirname(cookies), exist_ok=True)
    login_args = [ToolsList.get('biliup'), '-u', cookies, 'login']
    # ⚠️ 2026-11-16：原来是 `while not bili_islogin(...)` 无上限、无超时。
    #    cookies 失效又没人扫码时，会在这台 7x24 无人值守机器上
    #    无限 spawn 登录进程 —— 既不会成功，也不会报错，只是不停弹窗/写日志。
    #    与 biliuprs.login 对齐：最多 5 次，失败抛异常让上层看见。
    for attempt in range(1, 6):
        if bili_islogin(cookies):
            return cookies
        print(f'正在登录名称为 {account} 的B站账户:（第 {attempt}/5 次）')
        proc = subprocess.Popen(login_args)
        try:
            proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            print('登录超时（180 秒），已终止本次登录进程。')
    if bili_islogin(cookies):
        return cookies
    raise RuntimeError(
        f'{account} 的 B 站登录失败（已尝试 5 次）。请在有界面的机器上手动扫码，'
        f'或把 .login_info/{account}.json 拷到本机。')
    