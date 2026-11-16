import subprocess
import time
import os
import tempfile

__all__ = ['runcmd', 'run_and_capture_output']

class _CmdResult:
    """runcmd() 的轻量返回对象，只保留调用方真正用到的字段。

    Cleaner 的 ``method: custom`` 读的是 ``p.returncode``；把 output 一起带上
    方便排查（以前输出被 PIPE 吞了，失败了也看不到原因）。
    """
    __slots__ = ('returncode', 'output')

    def __init__(self, returncode, output=''):
        self.returncode = returncode
        self.output = output

    def __repr__(self):
        return f'<runcmd rc={self.returncode}>'


class runcmd():
    @staticmethod
    def runcmd(cmd, wait=True, timeout=None, **kwargs):
        """执行命令，返回带 ``.returncode`` / ``.output`` 的对象。

        2026-11-16 修（审查报告 L-20），原实现两个硬伤：
          ① ``shell = isinstance(cmd, str)``，但 ``_cmd`` 只在 ``not shell``
             分支里赋值 → 传字符串命令直接 UnboundLocalError（已证实）。
          ② ``stdout=PIPE`` 却只 ``wait()`` 不读管道 → 子进程输出超过管道
             缓冲（Windows 约 64KB）时双方互等死锁。Cleaner 的
             ``method: custom`` 走的正是这条路径。
        现在统一交给 ``run_and_capture_output``（重定向到临时文件，不死锁），
        字符串命令按 shell 语义执行。
        """
        if isinstance(cmd, str):
            argv = cmd
            kwargs.setdefault('shell', True)
        else:
            argv = [str(x) for x in cmd]
        if not wait:
            subprocess.Popen(argv, **kwargs)
            return _CmdResult(None, '')
        out, rc = run_and_capture_output(argv, timeout=timeout, **kwargs)
        return _CmdResult(rc, out)


def _output_tempfile(filename=None, dir=None):
    """优先用仓库约定的 ``.temp`` 目录，不可用时退回系统临时目录。"""
    candidates = []
    if dir:
        candidates.append(dir)
    else:
        candidates.extend(['.temp', None])
    for candidate in candidates:
        try:
            if candidate:
                os.makedirs(candidate, exist_ok=True)
            return tempfile.TemporaryFile(dir=candidate, prefix=filename or 'runcmd_')
        except OSError:
            continue
    return tempfile.TemporaryFile()


def _argv_of(cmd, kwargs:dict):
    """把 list / str 形式的命令统一成 Popen 能用的形式。

    2026-11-16（L-20）：原来这里无条件 ``[str(x) for x in cmd]``，
    传字符串命令会被拆成一个个字符。字符串走 shell，并把 shell=True
    写回 kwargs（调用方传进来的 kwargs 就地更新）。
    """
    if isinstance(cmd, str):
        kwargs.setdefault('shell', True)
        return cmd
    return [str(x) for x in cmd]


def run_and_capture_output(cmd:list, timeout:float=None, wait:bool=True,
                           decode:bool=True, filename:str=None, **kwargs) -> tuple:
    """执行子进程并返回 ``(合并输出, 退出码)``，不占内存也不死锁。

    为什么不用 ``stdout=subprocess.PIPE`` + ``proc.wait()``：

    1. 子进程写入量超过操作系统管道缓冲（Windows 约 64KB）时会**死锁** —— 子进程
       阻塞在写管道，父进程阻塞在 ``wait()``，而那段输出要等到 ``wait()`` 返回后才
       被读取。登录类命令（``biliup login``/``renew``）的输出正好可能超过这个量。
    2. ``bufsize=10**8`` 解决不了上面的问题（刷新仍发生在 ``wait()`` 之后），却会
       让每个子进程额外预留上百 MB 的缓冲内存。

    改成把输出重定向到**临时文件**后，子进程写文件不会被阻塞，``communicate()``
    负责收尾与回收，最后从文件读回字符串。

    :returns: ``(output, returncode)``。超时时进程被强杀，退出码为平台相关的
        非 0 值（Windows 上为 1），调用方的 ``returncode != 0`` 判定仍然成立。
    :param wait: 是否等待子进程结束（``False`` 时返回 ``('', None)``）。
    :param decode: 是否把输出解码为 ``str``。原调用方都会 ``.decode()``，
        保持 True 以避免下游对 bytes 调用 ``.lower()`` 这类隐蔽错误。
    """
    if not wait:
        subprocess.Popen(_argv_of(cmd, kwargs), **kwargs)
        return '', None

    argv = _argv_of(cmd, kwargs)
    with _output_tempfile(filename=filename) as logfile:
        proc = subprocess.Popen(
            argv,
            stdin=kwargs.pop('stdin', subprocess.DEVNULL),
            stdout=logfile,
            stderr=subprocess.STDOUT,
            **kwargs
        )
        try:
            proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        logfile.seek(0)
        out = logfile.read()
    return (out.decode('utf-8', errors='ignore') if decode else out), proc.returncode
