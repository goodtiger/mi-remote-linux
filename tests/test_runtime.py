"""语音进程单实例锁测试。"""

import pytest

from mi_remote_linux.runtime import AlreadyRunningError, VoiceInstanceLock


def test_voice_lock_rejects_second_instance_and_can_be_reacquired(tmp_path):
    path = tmp_path / "voice.lock"
    first = VoiceInstanceLock(path)
    second = VoiceInstanceLock(path)

    first.acquire()
    with pytest.raises(AlreadyRunningError):
        second.acquire()

    first.release()
    second.acquire()
    second.release()
