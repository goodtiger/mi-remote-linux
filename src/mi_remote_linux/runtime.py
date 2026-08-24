"""Linux 进程运行期辅助。"""

from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """另一个遥控器实例已持有运行锁。"""


class VoiceInstanceLock:
    """用 flock 防止两个进程同时占用同一个 BLE 语音通道。"""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
            path = runtime_dir / "mi-remote-linux-voice.lock"
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunningError("已有 mi-remote voice/keys 实例正在运行") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()
