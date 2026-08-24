"""ATVV 协议测试。"""

from mi_remote_linux.atvv import (
    ATVV_AUDIO_UUID,
    ATVV_CONTROL_UUID,
    ATVV_SERVICE_UUID,
    ATVV_TX_UUID,
    CODEC_16KHZ_MASK,
    DEFAULT_FRAME_SIZE,
    GET_CAPS_COMMAND,
    ATVVCapabilities,
    SyncFrame,
    make_mic_close_command,
    make_mic_open_command,
    parse_be16,
    parse_capabilities,
    parse_stream_session_id,
    parse_sync_frame,
)


class TestConstants:
    """常量测试。"""

    def test_uuids(self):
        """UUID 格式正确。"""
        assert len(ATVV_SERVICE_UUID) == 36
        assert len(ATVV_TX_UUID) == 36
        assert len(ATVV_AUDIO_UUID) == 36
        assert len(ATVV_CONTROL_UUID) == 36

    def test_get_caps_command(self):
        """GET_CAPS 命令格式正确。"""
        assert len(GET_CAPS_COMMAND) == 6
        assert GET_CAPS_COMMAND[0] == 0x0A


class TestParseBE16:
    """大端 16-bit 解析测试。"""

    def test_basic(self):
        """基本解析。"""
        assert parse_be16(bytes([0x00, 0x01]), 0) == 1
        assert parse_be16(bytes([0x01, 0x00]), 0) == 256
        assert parse_be16(bytes([0xFF, 0xFF]), 0) == 65535

    def test_offset(self):
        """带偏移解析。"""
        data = bytes([0xAA, 0x00, 0x01, 0xBB])
        assert parse_be16(data, 1) == 1

    def test_out_of_bounds(self):
        """越界返回 0。"""
        assert parse_be16(bytes([0x01]), 0) == 0
        assert parse_be16(bytes([]), 0) == 0


class TestMicCommands:
    """麦克风命令测试。"""

    def test_mic_open_v1(self):
        """v1.0+ MIC_OPEN 命令。"""
        cmd = make_mic_open_command(0x0100)
        assert cmd == bytes([0x0C, 0x00])

    def test_mic_open_v0(self):
        """v0.x MIC_OPEN 命令。"""
        cmd = make_mic_open_command(0x0000)
        assert cmd == bytes([0x0C, 0x00, 0x02])

    def test_mic_close_v1(self):
        """v1.0+ MIC_CLOSE 命令。"""
        cmd = make_mic_close_command(0x0100, 0x1234)
        assert cmd == bytes([0x0D, 0x34])

    def test_mic_close_v0(self):
        """v0.x MIC_CLOSE 命令。"""
        cmd = make_mic_close_command(0x0000, 0x1234)
        assert cmd == bytes([0x0D])


class TestCapabilities:
    """能力结构测试。"""

    def test_defaults(self):
        """默认值。"""
        caps = ATVVCapabilities()
        assert caps.protocol_version == 0
        assert caps.frame_size == DEFAULT_FRAME_SIZE
        assert caps.session_id == 0

    def test_parse_v1_layout(self):
        """v1 的 codec 位于 byte[3]，byte[4] 是 assistant model。"""
        caps = parse_capabilities(bytes([0x0B, 0x01, 0x00, 0x02, 0x7F, 0x00, 0x78]))
        assert caps == ATVVCapabilities(
            protocol_version=0x0100,
            codec_mask=CODEC_16KHZ_MASK,
            frame_size=120,
            selected_codec=CODEC_16KHZ_MASK,
        )

    def test_parse_legacy_codec_layout_and_default_frame_size(self):
        """兼容旧固件把 codec 放在 byte[4]，帧长 0 回退为 120。"""
        caps = parse_capabilities(bytes([0x0B, 0x00, 0x04, 0x00, 0x02, 0x00, 0x00]))
        assert caps is not None
        assert caps.codec_mask == CODEC_16KHZ_MASK
        assert caps.frame_size == DEFAULT_FRAME_SIZE

    def test_reject_malformed_or_unsupported_capabilities(self):
        assert parse_capabilities(bytes([0x0B, 0x01])) is None
        assert parse_capabilities(bytes([0x0A, 0x01, 0x00, 0x02, 0, 0, 120])) is None
        assert parse_capabilities(bytes([0x0B, 0x01, 0x00, 0x01, 0, 0, 120])) is None


class TestControlFrames:
    def test_parse_stream_session_id_uses_single_v1_byte(self):
        assert parse_stream_session_id(bytes([0x04, 0x00, 0x02, 0xA5])) == 0xA5
        assert parse_stream_session_id(bytes([0x04, 0x00, 0x02])) == 0

    def test_parse_sync_uses_v1_offsets_and_signed_predictor(self):
        sync = parse_sync_frame(bytes([0x0A, 0x02, 0x12, 0x34, 0xFF, 0x9C, 0x2A]))
        assert sync == SyncFrame(predictor=-100, step_index=42)

    def test_reject_malformed_sync(self):
        assert parse_sync_frame(bytes([0x0A, 0x02, 0, 0, 0, 0])) is None
        assert parse_sync_frame(bytes([0x00, 0x02, 0, 0, 0, 0, 0])) is None
