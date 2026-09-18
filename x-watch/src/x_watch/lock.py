"""单实例锁（方案 §3.1 / §10）。

手动运行和定时任务可能同时触发，必须保证只有一个进程写数据库。

实现用操作系统级文件锁（POSIX `fcntl.flock` / Windows `msvcrt.locking`）而不是
"锁文件是否存在"：进程崩溃时内核会自动释放，不会留下需要人工清理的僵尸锁。
"""

from __future__ import annotations

import os
from typing import Optional

from .util import ensure_dir

try:
    import fcntl  # POSIX

    _BACKEND = "fcntl"
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore
    try:
        import msvcrt  # type: ignore

        _BACKEND = "msvcrt"
    except ImportError:
        msvcrt = None  # type: ignore
        _BACKEND = "none"


class LockBusy(Exception):
    """已有进程持有锁。本轮应记为 skipped 并退出，而不是排队等待。"""


class SingleInstanceLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None  # type: Optional[object]

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        ensure_dir(os.path.dirname(os.path.abspath(self.path)))
        fh = open(self.path, "a+")
        try:
            if _BACKEND == "fcntl":
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif _BACKEND == "msvcrt":  # pragma: no cover - Windows
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - 理论上不会发生
                raise LockBusy("当前平台没有可用的文件锁实现，拒绝在无保护下写库")
        except LockBusy:
            fh.close()
            raise
        except OSError as exc:
            fh.close()
            holder = self._read_holder()
            raise LockBusy(
                "已有进程持有锁 %s%s：%s" % (self.path, holder, exc)
            ) from exc
        self._fh = fh
        try:
            fh.seek(0)
            fh.truncate()
            fh.write("pid=%d\n" % os.getpid())
            fh.flush()
        except OSError:
            pass  # 锁已经拿到了，写不进提示信息不影响正确性

    def _read_holder(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            return "（%s）" % text if text else ""
        except OSError:
            return ""

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            if _BACKEND == "fcntl":
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif _BACKEND == "msvcrt":  # pragma: no cover - Windows
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass
