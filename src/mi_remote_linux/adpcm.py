"""IMA ADPCM 解码器。

严格参考上游 macOS 项目：
mi_remote_control/Sources/MiRemote/Bluetooth/ADPCMDecoder.swift

标准 IMA/DVI ADPCM 解码（16kHz 单声道 16-bit，4:1 压缩）。
每字节高 nibble 在前、低 nibble 在后，各解一个样本。
"""

import numpy as np

# 89 级步长表
STEP_TABLE = np.array(
    [
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        16,
        17,
        19,
        21,
        23,
        25,
        28,
        31,
        34,
        37,
        41,
        45,
        50,
        55,
        60,
        66,
        73,
        80,
        88,
        97,
        107,
        118,
        130,
        143,
        157,
        173,
        190,
        209,
        230,
        253,
        279,
        307,
        337,
        371,
        408,
        449,
        494,
        544,
        598,
        658,
        724,
        796,
        876,
        963,
        1060,
        1166,
        1282,
        1411,
        1552,
        1707,
        1878,
        2066,
        2272,
        2499,
        2749,
        3024,
        3327,
        3660,
        4026,
        4428,
        4871,
        5358,
        5894,
        6484,
        7132,
        7845,
        8630,
        9493,
        10442,
        11487,
        12635,
        13899,
        15289,
        16818,
        18500,
        20350,
        22385,
        24623,
        27086,
        29794,
        32767,
    ],
    dtype=np.int32,
)

# 索引调整表
INDEX_TABLE = np.array([-1, -1, -1, -1, 2, 4, 6, 8], dtype=np.int32)


class ADPCMDecoder:
    """IMA ADPCM 解码器。

    支持同步帧重置（predictor + stepIndex），用于处理 ATVV 协议的 0x0A 同步帧。
    """

    def __init__(self):
        self.predictor: int = 0
        self.step_index: int = 0

    def reset(self, predictor: int, step_index: int) -> None:
        """用同步帧的 predictor/stepIndex 重置解码器状态。

        Args:
            predictor: Int16 范围的预测值
            step_index: 0-88 范围内的步长索引
        """
        self.predictor = int(np.clip(predictor, -32768, 32767))
        self.step_index = int(np.clip(step_index, 0, 88))

    def decode(self, data: bytes) -> np.ndarray:
        """解码一批 ADPCM 字节，返回 16-bit 样本数组。

        每字节产生 2 个样本：高 nibble 在前，低 nibble 在后。

        Args:
            data: ADPCM 编码的字节数据

        Returns:
            int16 类型的 numpy 数组，包含解码后的 PCM 样本
        """
        if not data:
            return np.array([], dtype=np.int16)

        # 预分配输出数组（每字节 2 个样本）
        output = np.zeros(len(data) * 2, dtype=np.int16)

        for i, byte in enumerate(data):
            # 高 nibble 在前
            output[i * 2] = self._decode_nibble(byte >> 4)

            # 低 nibble 在后
            output[i * 2 + 1] = self._decode_nibble(byte & 0x0F)

        return output

    def _decode_nibble(self, nibble: int) -> int:
        """解码单个 nibble，并推进跨帧保留的解码状态。"""
        step = int(STEP_TABLE[self.step_index])

        # diff = step * (mag + 0.5) 的整数展开
        diff = step >> 3
        if nibble & 4:
            diff += step
        if nibble & 2:
            diff += step >> 1
        if nibble & 1:
            diff += step >> 2

        # 符号位
        if nibble & 0x08:
            self.predictor -= diff
        else:
            self.predictor += diff

        # 钳到 Int16 范围
        self.predictor = min(max(self.predictor, -32768), 32767)

        # 更新步长索引
        self.step_index += int(INDEX_TABLE[nibble & 0x07])
        self.step_index = min(max(self.step_index, 0), 88)

        return self.predictor


class FrameAccumulator:
    """按帧长把 BLE 分包重组为完整帧。"""

    def __init__(self, frame_size: int = 120):
        self.frame_size = frame_size
        self.buffer = bytearray()

    def append(self, data: bytes) -> list[bytes]:
        """追加一段分包数据，返回已凑满的完整帧。"""
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= self.frame_size:
            frames.append(bytes(self.buffer[: self.frame_size]))
            self.buffer = self.buffer[self.frame_size :]
        return frames

    def reset(self) -> None:
        """丢弃缓冲中尚未凑满的残余半帧。"""
        self.buffer.clear()


class PCMPostprocessor:
    """PCM 后处理：三点平滑 + 增益。"""

    def __init__(self, gain_db: float = 0.0):
        self.gain = 10.0 ** (gain_db / 20.0)
        self.prev1: int | None = None
        self.prev2: int | None = None

    def set_gain(self, db: float) -> None:
        self.gain = 10.0 ** (db / 20.0)

    def reset(self) -> None:
        self.prev1 = None
        self.prev2 = None

    def process(self, samples: np.ndarray) -> np.ndarray:
        """处理一批样本，应用三点平滑和增益。"""
        if len(samples) == 0:
            return samples

        output = np.zeros_like(samples)

        for i in range(len(samples)):
            x0 = self.prev2
            x1 = self.prev1 if self.prev1 is not None else int(samples[i])
            x2 = int(samples[i])

            # 三点平滑 [1, 2, 1] / 4
            if x0 is not None:
                # Python // 对负数向下取整；int(/) 才与 Swift 整数除法的向零截断一致。
                smoothed = int((x0 + 2 * x1 + x2) / 4)
            else:
                smoothed = x2

            # 应用增益
            scaled = float(smoothed) * self.gain
            output[i] = int(np.clip(scaled, -32768, 32767))

            self.prev2 = self.prev1
            self.prev1 = x2

        return output
