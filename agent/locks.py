"""跨平台文件锁:POSIX 走 fcntl.flock,Windows 走 msvcrt.locking。

参考实现(s13 task store、s16 workflow run)直接 `import fcntl`,那是 Unix 专属。
本项目跑在 Windows,标准库又没有可移植的 advisory lock,这里统一包一层:
对约定好的锁文件在偏移 0 处锁 1 字节,所有持锁方用同一约定即互斥。

blocking 语义:
- nonblocking=False:拿不到就等(msvcrt.LK_LOCK / fcntl 阻塞 LOCK_EX)
- nonblocking=True :拿不到立即抛 OSError,调用方决定「已经在跑」该怎么做
"""
from __future__ import annotations

import os
from contextlib import contextmanager


def _acquire(fileobj, nonblocking: bool) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        fileobj.seek(0)
        mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
        msvcrt.locking(fileobj.fileno(), mode, 1)
    else:  # POSIX
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(fileobj.fileno(), flags)


def _release(fileobj) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path, nonblocking: bool = False):
    """独占锁 `path` 指向的锁文件。二进制打开:Windows 文本模式的 seek 有坑。"""
    directory = os.path.dirname(os.path.abspath(str(path)))
    os.makedirs(directory, exist_ok=True)
    handle = open(str(path), "a+b")
    try:
        # msvcrt.locking 锁的是「当前位置往后 N 字节」,空文件没字节可锁会抛
        # EACCES。占位 1 字节,让锁区间永远落在文件内(fcntl 无此烦恼,多写一字节无害)。
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\x00")
            handle.flush()
        _acquire(handle, nonblocking)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()