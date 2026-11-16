import os
import shutil
import platform
import subprocess


class move():
    @staticmethod
    def _trash_ok(ret):
        """把各平台"移入回收站"的返回值统一判成成功/失败。

        Windows 的 ``SHFileOperation`` 返回 (result, aborted)，0 才是成功；
        Linux/macOS 是 ``subprocess.call`` 的退出码。原实现忽略返回值，
        回收站操作失败也当成功，journal 照样把条目移除 → 文件其实还在。
        """
        if ret is None:
            return False
        if isinstance(ret, tuple):
            ret = ret[0] if ret else None
        try:
            return int(ret) == 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def move2Trash(deletePath):
        # windows
        if (platform.system() == 'Windows'):
            from win32com.shell import shell, shellcon
            return shell.SHFileOperation((0, shellcon.FO_DELETE, deletePath, None,
                                          shellcon.FOF_SILENT | shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION,
                                          None, None))
        # linux
        elif (platform.system() == 'Linux'):
            cmd = ['trash', deletePath]
            return subprocess.call(cmd, stdout=open(os.devnull, 'w'))
        # macOS
        elif (platform.system() == 'Darwin'):
            absPath = os.path.abspath(deletePath).replace(
                '\\', '\\\\').replace('"', '\\"')
            cmd = ['osascript', '-e',
                   'tell app "Finder" to move {the POSIX file "' + absPath + '"} to trash']
            return subprocess.call(cmd, stdout=open(os.devnull, 'w'))

    @staticmethod
    def move(src, dst, mkdir=True, **kwargs):
        if not dst:
            raise ValueError('移动路径不能为空')
        elif dst == '*TRASHBIN*':
            ret = move.move2Trash(src)
            if not move._trash_ok(ret):
                raise RuntimeError(f'移入回收站失败（返回值 {ret!r}）: {src}')
            return

        if not os.path.exists(dst):
            if mkdir:
                os.makedirs(dst)
            else:
                raise ValueError(f'Path not exists: {dst}')

        # 目标已存在同名文件时加 (n) 后缀。
        # shutil.move 对"目标是已存在的文件"会静默覆盖（内部 copy2+unlink），
        # 清理循环里两个分段同名就会互相顶掉。
        target = dst
        if os.path.isdir(dst):
            base = os.path.basename(src)
            target = os.path.join(dst, base)
            if os.path.exists(target):
                stem, ext = os.path.splitext(base)
                i = 1
                while os.path.exists(os.path.join(dst, f'{stem}({i}){ext}')):
                    i += 1
                target = os.path.join(dst, f'{stem}({i}){ext}')
        elif os.path.exists(dst):
            stem, ext = os.path.splitext(dst)
            i = 1
            while os.path.exists(f'{stem}({i}){ext}'):
                i += 1
            target = f'{stem}({i}){ext}'
        return shutil.move(src, target)
