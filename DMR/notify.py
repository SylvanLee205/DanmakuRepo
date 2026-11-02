"""推送通知模块（Server酱³ / PushDeer）。

⚠️ 换行格式（2026-10-07 实测确定）：
    Server酱³ 按 **Markdown** 解析 desp。
        - 纯 "\\n"           -> 手机列表页看着像换行，点进"原文"就没了
        - "<br>"             -> 原样显示成文本，无效
        - "两空格 + \\n"     -> ✅ 正确换行（Markdown 硬换行语法）
    所以本模块统一用 `_nl()` 生成换行：'  \\n'（两个空格 + 换行）。

⚠️ 并发（2026-11-02 修）：
    原来标题/正文写到项目根的 `notify_title.txt` / `notify_content.txt` ——
    **固定路径**。同时有两条通知（两个主播同时开播、启动通知与开播通知撞一起）
    就会互相覆盖：A 线程准备的正文可能被 B 覆盖，发出去的内容是错的，
    或者 Server酱 直接报错。现在每次发送用**独立的临时目录**，天然并发安全。

⚠️ 失败原因（2026-11-02 修）：
    `send()` 以前只返回 True/False，把 curl 的响应**整个丢掉**。
    2026-11-02 14:40 有一次开播通知失败，日志只写"未返回 code:0"，
    完全看不出是超时、额度用完还是服务端 5xx。现在失败原因记在
    ``last_error`` 里，调用方可以打出来。

用法：
    from DMR.notify import send, last_error
    if not send('标题', ['第一行', '第二行']):
        logger.warning('通知失败：%s', last_error)
"""
import os
import subprocess
import sys
import tempfile
import time

API_URL = ('https://17530.push.ft07.com/send/'
           'sctp17530thl3urer2dsbnw3rnqyxzep.send')

# Markdown 硬换行：两个空格 + 换行符
# 为什么不能用 <br>：实测会原样显示成 "<br>" 文本
_NL = '  \n'

#: 最近一次发送失败的原因（成功时清空）。
#: 为什么用模块级变量而不是返回值：调用点很多，改签名要动一圈；
#: 而且失败原因只是给人看的，不影响控制流。
last_error = ''

#: 网络类失败重试次数（服务端明确拒绝时**不重试**，免得白白消耗额度）
_RETRIES = 2
_RETRY_DELAY = 2.0


def _join(lines):
    """把多行拼成 Server酱³ 能正确换行的格式。"""
    return _NL.join(str(x) for x in lines)


def _curl_exe():
    """curl 的可执行路径。

    Windows: 用 System32\\curl.exe 的绝对路径
             （session 0 / 精简 PATH 下裸 'curl.exe' 会找不到）。
    POSIX  : curl 就在 PATH 里，且**不带 .exe 后缀** ——
             直接写 'curl.exe' 在 Linux/mac 上必然失败。
    """
    if sys.platform != 'win32':
        import shutil
        return shutil.which('curl') or 'curl'
    try:
        from .paths import system32
        return system32('curl.exe')
    except Exception:
        return 'curl.exe'


def _post(title, content, timeout):
    """发一次请求，返回 (ok, detail)。detail 是给人看的诊断信息。"""
    # 每次发送用独立临时目录 —— 并发安全，且不污染项目根
    with tempfile.TemporaryDirectory(prefix='dmr_notify_') as td:
        tpath = os.path.join(td, 'title.txt')
        dpath = os.path.join(td, 'desp.txt')
        with open(tpath, 'w', encoding='utf-8', newline='') as f:
            f.write(title)
        with open(dpath, 'w', encoding='utf-8', newline='') as f:
            f.write(content)

        r = subprocess.run(
            [_curl_exe(), '-s', '-m', str(timeout),
             # 顺手把 HTTP 状态码打出来，方便区分"超时"和"服务端报错"
             '-w', '\\nHTTP:%{http_code}',
             '--data-urlencode', f'title@{tpath}',
             '--data-urlencode', f'desp@{dpath}',
             API_URL],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=timeout + 10,
            stdin=subprocess.DEVNULL)

    out = (r.stdout or '').strip()
    err = (r.stderr or '').strip()
    if '"code":0' in out:
        return True, 'OK'
    return False, f'curl rc={r.returncode} 响应={out[:300] or "(空)"} 错误={err[:200] or "-"}'


def send(title, lines, timeout=15):
    """发送通知。lines 可以是字符串列表，会自动按 Markdown 硬换行拼接。

    失败**不抛异常** —— 通知不能影响录制主流程。
    返回 True/False；失败原因在模块级 ``last_error`` 里。
    """
    global last_error
    try:
        content = lines if isinstance(lines, str) else _join(lines)
    except Exception as e:
        last_error = f'准备内容失败: {e!r}'
        return False

    detail = ''
    for attempt in range(_RETRIES + 1):
        try:
            ok, detail = _post(title, content, timeout)
        except subprocess.TimeoutExpired:
            ok, detail = False, f'请求超时（>{timeout}s）'
        except Exception as e:
            ok, detail = False, f'调用 curl 失败: {e!r}'

        if ok:
            last_error = ''
            return True

        # 服务端明确拒绝（有 HTTP 响应且含错误码）就不要重试了 ——
        # 重试只会重复消耗额度，结果还是失败。
        if 'HTTP:4' in detail or 'HTTP:5' in detail:
            break
        if attempt < _RETRIES:
            time.sleep(_RETRY_DELAY)

    last_error = detail
    return False


def disk_free_gb(path=None):
    """磁盘剩余空间（GB）。返回 None 表示查不到。

    ⚠️ 2026-11-02：原来默认写死 'F:\\'。项目换到别的盘（或录制盘独立）时
    这个值会报错误的盘 —— 磁盘告警就会误判。现在默认取**数据所在盘**
    （DMR_DATA_ROOT，缺省为项目根）。
    """
    try:
        if path is None:
            from .paths import data_root
            path = data_root()
        import shutil
        return shutil.disk_usage(path).free / 1024 / 1024 / 1024
    except Exception:
        return None


if __name__ == '__main__':
    # 手动测试：python -m DMR.notify
    ok = send('DanmakuRepo 通知测试',
              ['第一行', '第二行（应该有换行）', '', '第四行'])
    print(f'发送{"成功" if ok else "失败"}')
    if not ok:
        print(f'原因: {last_error}')
