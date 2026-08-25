"""语音排空跟踪器：在语音键松开后等待尾部静默再释放会话。

语义等价于 macOS 的 VoiceDrainTracker，防止最后 ~20ms 的音频被截断：
- 总超时 170ms：从释放开始算起，最多等 170ms 后强制释放
- 尾部静默 20ms：最后一次收到新数据后再等 20ms 确认无新数据
- 调用方每约 10ms 轮询一次 should_release

关键设计：
- 跟踪"最后一次收到新数据的时间"，而非缓冲区大小
- 任何通过 generation/streaming 检查的非空 AUDIO notify 都算新数据
- 即使数据不足以拼成完整帧（残片），也算新数据到达
"""

from __future__ import annotations

from dataclasses import dataclass

# 常量，与 macOS 参考实现保持一致
OUTPUT_TAIL_SECONDS: float = 0.020  # 20ms 尾部静默确认
TIMEOUT_SECONDS: float = 0.170  # 170ms 总超时


@dataclass
class VoiceDrainTracker:
    """纯数据跟踪器，不依赖 asyncio；由调用方提供单调时间。"""

    deadline: float = 0.0
    last_activity: float = 0.0

    def start(self, now: float) -> None:
        """记录释放时刻，开始排空倒计时。"""
        self.deadline = now + TIMEOUT_SECONDS
        self.last_activity = now  # 初始化为开始时间，视为最后一次活动

    def on_activity(self, now: float) -> None:
        """记录收到新数据（任何通过检查的非空 AUDIO notify）。"""
        self.last_activity = now

    def should_release(self, now: float) -> bool:
        """判断是否可以安全释放。

        Args:
            now: 当前单调时间戳。

        Returns:
            True 表示可以关闭通道并触发转写。
        """
        # 1. 总超时 → 强制释放
        if now >= self.deadline:
            return True

        # 2. 尾部静默 ≥ 20ms → 确认排空完成
        return (now - self.last_activity) >= OUTPUT_TAIL_SECONDS
