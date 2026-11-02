"""推送通知模块（Server酱³，requests 实现 + 双端点回退）。

⚠️ 换行格式（2026-10-07 实测确定）：
    Server酱³ 按 **Markdown** 解析 desp。
        - 纯 "\\n"           -> 手机列表页看着像换行，点进"原文"就没了
        - "<br>"             -> 原样显示成文本，无效
        - "两空格 + \\n"     -> ✅ 正确换行（Markdown 硬换行语法）
    所以本模块统一用 ``_NL`` 生成换行：'  \\n'（两个空格 + 换行）。

⚠️ 为什么从 curl 改成 requests（2026-11-02 晚实盘）：
    原来用 ``subprocess`` 调 ``System32\\curl.exe``（Schannel TLS）发 POST。
    当晚发现 curl 对 ``push.ft07.com`` 的 TLS 握手直接失败（rc=35，HTTP 000），
    而同一台机器上 requests（OpenSSL + certifi）对 ``sctapi.ftqq.com`` 秒通
    —— **curl 的 Schannel 握不上，OpenSSL 握得上**，跟网络代理的 TLS 指纹
    处理有关。curl 还受 PATH 影响（session 0 找不到），requests 是进程内
    库、无外部依赖，稳定性高一个量级。curl 只留作 requests 不可用时的兜底。

⚠️ 双端点（2026-11-02 晚实盘）：
    Server酱³ 有两个等价入口：
        https://<uid>.push.ft07.com/send/<key>.send
        https://sctapi.ftqq.com/<key>.send
    当晚 ft07 被本机代理的 TUN 出口掐了 TLS（SSL EOF），sctapi 却正常。
    两个都试，谁通用谁 —— 单端点在"网络环境半坏"时通知全灭，用户
    最先注意到的就是"怎么又没通知了"。

⚠️ 失败原因（2026-11-02 修）：
    ``send()`` 只返回 True/False，但失败原因记在模块级 ``last_error`` 里，
    调用方可以打出来（14:40 有一次失败，日志只写"未返回 code:0"，
    完全看不出是超时、额度用完还是 5xx）。

⚠️ **凭据不再硬编码（2026-11-03 修）**：
    原来 Server酱³ 的 key 直接写在下面的 URL 常量里，而这个文件在**公开仓库**
    `SylvanLee205/DanmakuRepo` 里 —— 任何拿到仓库的人都能拿你的额度发通知。
    现在按优先级读取：
        1) 环境变量 ``DMR_SCT_KEY``（可选 ``DMR_SCT_UID``）
        2) ``configs/global.yml`` 的 ``notify.sct_key`` / ``notify.sct_uid``
        3) ``.login_info/notify_key.txt``（第一行 key，第二行可放 uid）
    ⚠️ **旧 key 已经进 git 历史**，光改代码不够 —— 必须去 Server酱³ 后台
    重置（换一个 key），历史里那个就作废了。重置后把新 key 写进
    ``configs/global.yml``（不入库）或环境变量。

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


def _load_credentials():
    """取 (uid, key)。都取不到返回 (None, None)。

    uid 只是数字（Server酱³ 账号 id），key 才是秘密。
    """
    uid = os.environ.get('DMR_SCT_UID', '').strip()
    key = os.environ.get('DMR_SCT_KEY', '').strip()

    if not key:
        # 2) configs/global.yml
        try:
            from .paths import CONFIGS
            import yaml
            gpath = os.path.join(CONFIGS, 'global.yml')
            if os.path.isfile(gpath):
                with open(gpath, encoding='utf-8') as f:
                    g = yaml.safe_load(f) or {}
                n = (g.get('notify') or {})
                key = str(n.get('sct_key') or '').strip()
                uid = uid or str(n.get('sct_uid') or '').strip()
        except Exception:
            pass

    if not key:
        # 3) .login_info/notify_key.txt
        try:
            from .paths import ROOT
            p = os.path.join(ROOT, '.login_info', 'notify_key.txt')
            if os.path.isfile(p):
                with open(p, encoding='utf-8') as f:
                    lines = [x.strip() for x in f.read().splitlines() if x.strip()]
                if lines:
                    key = lines[0]
                    if len(lines) > 1 and not uid:
                        uid = lines[1]
        except Exception:
            pass

    return (uid or None), (key or None)


#: (uid, key) —— 从配置/环境变量取，不在代码里
_SCT_UID, _SCT_KEY = _load_credentials()


def _build_api_urls(uid, key):
    """两个等价端点，按顺序试。

        https://<uid>.push.ft07.com/send/<key>.send
        https://sctapi.ftqq.com/<key>.send

    单端点在"网络环境半坏"时通知全灭，用户最先注意到的就是"怎么又没通知了"。
    """
    if not key:
        return []
    urls = []
    if uid:
        urls.append(f'https://{uid}.push.ft07.com/send/{key}.send')
    urls.append(f'https://sctapi.ftqq.com/{key}.send')
    return urls


#: 两个等价的 Server酱³ 端点，按顺序试。
API_URLS = _build_api_urls(_SCT_UID, _SCT_KEY)
#: 兼容旧名字（有代码按 API_URL 引用）
API_URL = API_URLS[0] if API_URLS else ''

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


def _post_requests(title, content, timeout):
    """requests 直发（首选通道），返回 (ok, detail)。"""
    import requests
    details = []
    for url in API_URLS:
        try:
            r = requests.post(url, data={'title': title, 'desp': content},
                              timeout=timeout)
        except Exception as e:
            details.append(f'{url.split("/")[2]}: {type(e).__name__}: {e}'[:160])
            continue
        if r.status_code == 200 and '"code":0' in (r.text or ''):
            return True, 'OK'
        details.append(f'{url.split("/")[2]}: HTTP {r.status_code} '
                       f'{(r.text or "")[:120]}')
    return False, '；'.join(details)


def _curl_exe():
    """curl 的可执行路径（仅兜底用）。

    Windows: 用 System32\\curl.exe 的绝对路径
             （session 0 / 精简 PATH 下裸 'curl.exe' 会找不到）。
    POSIX  : curl 就在 PATH 里，且**不带 .exe 后缀**。
    """
    if sys.platform != 'win32':
        import shutil
        return shutil.which('curl') or 'curl'
    try:
        from .paths import system32
        return system32('curl.exe')
    except Exception:
        return 'curl.exe'


def _post_curl(title, content, timeout):
    """curl 兜底通道（requests 不可用时才走到），返回 (ok, detail)。"""
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
             '-w', '\nHTTP:%{http_code}',
             '--data-urlencode', f'title@{tpath}',
             '--data-urlencode', f'desp@{dpath}',
             API_URLS[0]],
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
    if not API_URLS:
        last_error = ('未配置 Server酱³ key：设环境变量 DMR_SCT_KEY，'
                      '或写 configs/global.yml 的 notify.sct_key')
        return False

    try:
        content = lines if isinstance(lines, str) else _join(lines)
    except Exception as e:
        last_error = f'准备内容失败: {e!r}'
        return False

    from importlib.util import find_spec
    poster = _post_requests if find_spec('requests') else _post_curl

    detail = ''
    for attempt in range(_RETRIES + 1):
        try:
            ok, detail = poster(title, content, timeout)
        except Exception as e:
            ok, detail = False, f'发送异常: {e!r}'

        if ok:
            last_error = ''
            return True

        # 服务端明确拒绝（有 HTTP 响应且非网络错误）就不要重试了 ——
        # 重试只会重复消耗额度，结果还是失败。
        if 'HTTP 4' in detail or 'HTTP 5' in detail or 'HTTP:4' in detail \
                or 'HTTP:5' in detail:
            break
        if attempt < _RETRIES:
            time.sleep(_RETRY_DELAY)

    last_error = detail
    return False


def disk_free_gb(path=None):
    """磁盘剩余空间（GB）。返回 None 表示查不到。"""
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
