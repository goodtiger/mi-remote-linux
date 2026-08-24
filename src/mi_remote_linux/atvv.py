"""ATVV 协议常量、值对象和纯解析函数。

严格参考上游 macOS 项目：
mi_remote_control/Sources/MiRemote/App/Contracts.swift
"""

from dataclasses import dataclass

# ATVV BLE GATT UUIDs
ATVV_SERVICE_UUID = "AB5E0001-5A21-4F05-BC7D-AF01F617B664"
ATVV_TX_UUID = "AB5E0002-5A21-4F05-BC7D-AF01F617B664"  # 主机写命令
ATVV_AUDIO_UUID = "AB5E0003-5A21-4F05-BC7D-AF01F617B664"  # 音频帧 notify
ATVV_CONTROL_UUID = "AB5E0004-5A21-4F05-BC7D-AF01F617B664"  # 控制 notify

# 电池服务（可选）
BATTERY_SERVICE_UUID = "0000180F-0000-1000-8000-00805F9B34FB"
BATTERY_LEVEL_UUID = "00002A19-0000-1000-8000-00805F9B34FB"

# 遥控器设备信息
REMOTE_VENDOR_ID = 0x2717
REMOTE_PRODUCT_ID = 0x32B8

# ATVV 协议操作码
OP_CAPS = 0x0B  # 能力帧
OP_MIC_REQUEST = 0x08  # 用户按下语音键
OP_AUDIO_START = 0x04  # 流开始
OP_SYNC = 0x0A  # 同步帧
OP_AUDIO_STOP = 0x00  # 流结束

# GET_CAPS 命令
GET_CAPS_COMMAND = bytes([0x0A, 0x01, 0x00, 0x00, 0x03, 0x03])

# 默认帧长
DEFAULT_FRAME_SIZE = 120

# 16kHz codec 位掩码
CODEC_16KHZ_MASK = 0x02


@dataclass(eq=True)
class ATVVCapabilities:
    """ATVV 握手协商结果。"""

    protocol_version: int = 0
    codec_mask: int = 0
    frame_size: int = DEFAULT_FRAME_SIZE
    selected_codec: int = 0
    session_id: int = 0


@dataclass(eq=True, frozen=True)
class SyncFrame:
    """同步帧数据（用于重置 ADPCM 解码器）。"""

    predictor: int  # Int16
    step_index: int  # 0-88


def make_mic_open_command(protocol_version: int) -> bytes:
    """生成 MIC_OPEN 命令。

    版本 >= 0x0100 → [0x0C, 0x00]
    否则 → [0x0C, 0x00, 0x02]
    """
    if protocol_version >= 0x0100:
        return bytes([0x0C, 0x00])
    else:
        return bytes([0x0C, 0x00, 0x02])


def make_mic_close_command(protocol_version: int, session_id: int) -> bytes:
    """生成 MIC_CLOSE 命令。

    版本 >= 0x0100 → [0x0D] + stream ID 低字节
    否则 → [0x0D]
    """
    payload = [0x0D]
    if protocol_version >= 0x0100:
        payload.append(session_id & 0xFF)
    return bytes(payload)


def parse_be16(data: bytes, offset: int) -> int:
    """从字节数组读取大端 16-bit 整数。"""
    if offset + 1 >= len(data):
        return 0
    return (data[offset] << 8) | data[offset + 1]


def parse_capabilities(data: bytes) -> ATVVCapabilities | None:
    """解析 0x0B 能力帧，并确认遥控器支持 16 kHz ADPCM。

    ATVV v1 把 codec 掩码放在 byte[3]。部分旧固件放在 byte[4]，仅当
    byte[3] 没有 16 kHz 位时才使用该兼容布局。
    """
    if len(data) < 7 or data[0] != OP_CAPS:
        return None

    codec_mask = data[3]
    if not codec_mask & CODEC_16KHZ_MASK and data[4] & CODEC_16KHZ_MASK:
        codec_mask = data[4]
    if not codec_mask & CODEC_16KHZ_MASK:
        return None

    frame_size = parse_be16(data, 5) or DEFAULT_FRAME_SIZE
    return ATVVCapabilities(
        protocol_version=parse_be16(data, 1),
        codec_mask=codec_mask,
        frame_size=frame_size,
        selected_codec=CODEC_16KHZ_MASK,
    )


def parse_stream_session_id(data: bytes) -> int:
    """从 v1 AUDIO_START 帧解析单字节 stream ID。"""
    if len(data) < 4 or data[0] != OP_AUDIO_START:
        return 0
    return data[3]


def parse_sync_frame(data: bytes) -> SyncFrame | None:
    """解析 v1 SYNC 帧的有符号 predictor 和 step index。"""
    if len(data) < 7 or data[0] != OP_SYNC:
        return None

    predictor = parse_be16(data, 4)
    if predictor >= 0x8000:
        predictor -= 0x10000
    return SyncFrame(predictor=predictor, step_index=data[6])
