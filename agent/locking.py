"""同一部署目录内跨进程互斥；进程退出时操作系统自动释放锁。"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


class OperationBusyError(RuntimeError):
    pass


@contextmanager
def exclusive_lock(name):
    directory = Path(os.getenv("APP_LOCK_DIR", str(Path(__file__).resolve().parents[1] / ".runtime" / "locks")))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(name.encode()).hexdigest() + ".lock")
    with path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OperationBusyError("操作正在执行，请稍后重试") from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
