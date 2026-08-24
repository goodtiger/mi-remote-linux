"""ADPCM 解码器测试。"""

import numpy as np

from mi_remote_linux.adpcm import ADPCMDecoder, FrameAccumulator, PCMPostprocessor


class TestADPCMDecoder:
    """ADPCM 解码器测试。"""

    def test_decode_empty(self):
        """空数据解码。"""
        decoder = ADPCMDecoder()
        result = decoder.decode(b"")
        assert len(result) == 0

    def test_decode_single_byte(self):
        """单字节解码产生 2 个样本。"""
        decoder = ADPCMDecoder()
        decoder.reset(0, 0)
        result = decoder.decode(bytes([0x00]))
        assert len(result) == 2
        assert result.dtype == np.int16

    def test_reset(self):
        """重置解码器状态。"""
        decoder = ADPCMDecoder()
        decoder.decode(bytes([0x55] * 10))  # 改变状态

        decoder.reset(1000, 10)
        assert decoder.predictor == 1000
        assert decoder.step_index == 10

    def test_reset_clamp(self):
        """重置时钳到有效范围。"""
        decoder = ADPCMDecoder()

        decoder.reset(50000, 100)  # 超出范围
        assert decoder.predictor == 32767
        assert decoder.step_index == 88

        decoder.reset(-50000, -10)
        assert decoder.predictor == -32768
        assert decoder.step_index == 0

    def test_decode_silence(self):
        """全零数据解码（静音）。"""
        decoder = ADPCMDecoder()
        decoder.reset(0, 0)
        result = decoder.decode(bytes([0x00] * 120))
        assert len(result) == 240
        # 静音应该产生接近 0 的值
        assert np.abs(result).max() < 100

    def test_nibbles_advance_step_index(self):
        """每个 nibble 都必须推进状态，覆盖旧实现丢失 step_index 的回归。"""
        decoder = ADPCMDecoder()
        decoder.reset(0, 0)

        result = decoder.decode(bytes([0x77]))

        assert result.tolist() == [11, 41]
        assert decoder.predictor == 41
        assert decoder.step_index == 16

    def test_decode_state_continues_across_batches(self):
        whole = ADPCMDecoder().decode(bytes([0x12, 0x34, 0x56, 0x78]))

        split_decoder = ADPCMDecoder()
        split = np.concatenate(
            [split_decoder.decode(bytes([0x12, 0x34])), split_decoder.decode(bytes([0x56, 0x78]))]
        )

        np.testing.assert_array_equal(split, whole)

    def test_predictor_and_step_index_saturate(self):
        decoder = ADPCMDecoder()
        decoder.reset(0, 80)
        result = decoder.decode(bytes([0x77] * 50))

        assert result[-1] == 32767
        assert decoder.step_index == 88


class TestFrameAccumulator:
    """帧累加器测试。"""

    def test_single_frame(self):
        """单帧完整接收。"""
        acc = FrameAccumulator(frame_size=120)
        frames = acc.append(bytes(120))
        assert len(frames) == 1
        assert len(frames[0]) == 120

    def test_partial_frame(self):
        """分包接收。"""
        acc = FrameAccumulator(frame_size=120)

        # 第一包 50 字节
        frames = acc.append(bytes(50))
        assert len(frames) == 0

        # 第二包 70 字节，凑满一帧
        frames = acc.append(bytes(70))
        assert len(frames) == 1
        assert len(frames[0]) == 120

    def test_multiple_frames(self):
        """多帧接收。"""
        acc = FrameAccumulator(frame_size=120)

        # 一次给 360 字节 = 3 帧
        frames = acc.append(bytes(360))
        assert len(frames) == 3

    def test_reset(self):
        """重置缓冲。"""
        acc = FrameAccumulator(frame_size=120)
        acc.append(bytes(50))  # 半帧
        acc.reset()
        assert len(acc.buffer) == 0


class TestPCMPostprocessor:
    """PCM 后处理器测试。"""

    def test_passthrough(self):
        """零增益直通。"""
        proc = PCMPostprocessor(gain_db=0.0)
        samples = np.array([0, 1000, -1000, 32767, -32768], dtype=np.int16)
        result = proc.process(samples)
        assert len(result) == len(samples)
        # 零增益时应该接近原值（第一个样本无历史，直通）
        assert result[0] == 0

    def test_gain(self):
        """增益放大。"""
        proc = PCMPostprocessor(gain_db=6.0)  # 约 2 倍
        samples = np.array([1000], dtype=np.int16)
        result = proc.process(samples)
        # 第一个样本无平滑，只应用增益
        assert abs(result[0]) > abs(samples[0])

    def test_reset(self):
        """重置平滑历史。"""
        proc = PCMPostprocessor()
        proc.process(np.array([1000, 2000], dtype=np.int16))
        proc.reset()
        assert proc.prev1 is None
        assert proc.prev2 is None

    def test_smoothing_is_continuous_across_batches(self):
        proc = PCMPostprocessor(gain_db=0.0)
        first = proc.process(np.array([100, 200, 300], dtype=np.int16))
        second = proc.process(np.array([400, 500], dtype=np.int16))

        assert first.tolist() == [100, 200, 200]
        assert second.tolist() == [300, 400]

    def test_smoothing_uses_wide_math_and_truncates_toward_zero(self):
        proc = PCMPostprocessor(gain_db=0.0)
        proc.prev2 = -1
        proc.prev1 = 0

        result = proc.process(np.array([0, 32767], dtype=np.int16))

        assert result[0] == 0
        assert result[1] == 8191
