"""Linux 焦点文本注入测试。"""

import pytest

from mi_remote_linux.injector import LinuxTextInjector, TextInjectionError


class RecordingInjector(LinuxTextInjector):
    def __init__(
        self,
        *,
        session: str = "wayland",
        paste_shortcut: str = "ctrl-v",
        submit: bool = False,
        terminal: bool = False,
    ):
        super().__init__(
            session=session,
            paste_shortcut=paste_shortcut,
            submit=submit,
            environment={},
            wl_copy="wl-copy",
            wtype="wtype",
            xclip="xclip",
            xdotool="xdotool",
            hyprctl="",
            paste_delay=0,
        )
        self.calls = []
        self.error_on_call = None
        self.terminal = terminal

    async def _run(
        self,
        *command: str,
        input_data: bytes | None = None,
        clipboard_owner: bool = False,
    ) -> None:
        self.calls.append((command, input_data))
        if self.error_on_call == len(self.calls):
            raise TextInjectionError("test failure")

    async def _active_window_is_terminal(self) -> bool:
        return self.terminal


def test_auto_detects_wayland_before_x11():
    injector = LinuxTextInjector(
        environment={"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"},
        wl_copy="wl-copy",
        wtype="wtype",
    )

    assert injector.session == "wayland"


def test_reports_missing_dependencies_for_each_session():
    wayland = LinuxTextInjector(session="wayland", environment={}, wl_copy="", wtype="")
    x11 = LinuxTextInjector(session="x11", environment={}, xclip="", xdotool="")

    assert wayland.missing_dependencies() == ["wl-copy", "wtype"]
    assert x11.missing_dependencies() == ["xclip", "xdotool"]


@pytest.mark.asyncio
async def test_wayland_copies_utf8_then_sends_ctrl_v():
    injector = RecordingInjector()

    await injector.inject("你好，Linux")

    assert injector.calls == [
        (("wl-copy", "--type", "text/plain;charset=utf-8"), "你好，Linux".encode()),
        (("wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"), None),
    ]


@pytest.mark.asyncio
async def test_x11_copies_utf8_then_pastes():
    injector = RecordingInjector(session="x11")

    await injector.inject("跨桌面")

    assert injector.calls == [
        (("xclip", "-selection", "clipboard", "-in"), "跨桌面".encode()),
        (("xdotool", "key", "--clearmodifiers", "ctrl+v"), None),
    ]


@pytest.mark.asyncio
async def test_auto_uses_ctrl_shift_v_for_terminal():
    injector = RecordingInjector(paste_shortcut="auto", terminal=True)

    await injector.inject("终端输入")

    assert injector.calls[-1] == (
        ("wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"),
        None,
    )


@pytest.mark.asyncio
async def test_submit_is_explicit_and_happens_after_paste():
    injector = RecordingInjector(submit=True)

    await injector.inject("发送这句话")

    assert injector.calls[-1] == (("wtype", "-k", "Return"), None)


@pytest.mark.asyncio
async def test_copy_failure_does_not_send_paste_shortcut():
    injector = RecordingInjector()
    injector.error_on_call = 1

    with pytest.raises(TextInjectionError, match="test failure"):
        await injector.inject("不会被粘贴")

    assert len(injector.calls) == 1


@pytest.mark.asyncio
async def test_paste_failure_reports_that_transcript_is_in_clipboard():
    injector = RecordingInjector()
    injector.error_on_call = 2

    with pytest.raises(TextInjectionError) as caught:
        await injector.inject("仍可手动粘贴")

    assert caught.value.clipboard_ready is True
