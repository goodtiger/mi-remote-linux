"""Tap/hold/double/gesture/layer state machine for remote buttons."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config import Action, KeyBinding, MappingConfig
from .hid_engine import ButtonEvent

logger = logging.getLogger(__name__)


@dataclass
class _KeyState:
    phase: str = "idle"
    sequence: int = 0
    hold_fired: bool = False
    suppress_tap: bool = False
    timer: asyncio.TimerHandle | None = None


class MappingEngine:
    """Pure event state machine; hardware and desktop output are injected."""

    def __init__(
        self,
        config: MappingConfig,
        run_action: Callable[[Action], Awaitable[None]],
        *,
        on_layer: Callable[[int], None] | None = None,
        on_escape: Callable[[], None] | None = None,
        event_filter: Callable[[ButtonEvent], bool] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        self.config = config
        self.run_action = run_action
        self.on_layer = on_layer
        self.on_escape = on_escape
        self.event_filter = event_filter
        self._loop = loop
        self._states: dict[str, _KeyState] = {}
        self._locked_layer = 0
        self._momentary_layer: int | None = None
        self._momentary_owner: str | None = None
        self._layer_timer: asyncio.TimerHandle | None = None
        self._escape_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_layer = 0
        self._active_profile: str | None = None

    @property
    def effective_layer(self) -> int:
        return self._momentary_layer if self._momentary_layer is not None else self._locked_layer

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def handle(self, event: ButtonEvent) -> None:
        if self._locked_layer:
            self._restart_layer_idle()
        if event.key == "menu":
            self._track_escape(event.is_down)
        if self.event_filter and self.event_filter(event):
            if not event.is_down:
                self._release_filtered_key(event.key)
            return
        if event.is_down:
            self._handle_down(event.key)
        else:
            self._handle_up(event.key)

    def reset(self, reason: str = "input reset") -> None:
        for state in self._states.values():
            state.sequence += 1
            self._cancel(state.timer)
            state.timer = None
            state.phase = "idle"
            state.hold_fired = False
            state.suppress_tap = False
        self._cancel(self._escape_timer)
        self._escape_timer = None
        self._cancel(self._layer_timer)
        self._layer_timer = None
        self._locked_layer = 0
        self._momentary_layer = None
        self._momentary_owner = None
        self._notify_layer()
        logger.info("mapping state reset: %s", reason)

    async def close(self) -> None:
        self.reset("closed")
        self._cancel(self._layer_timer)
        self._layer_timer = None
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def set_active_application(self, application: str | None) -> None:
        """Select the first profile whose app pattern matches the active desktop app."""
        normalized = (application or "").casefold()
        selected = None
        for profile, patterns in self.config.profile_apps.items():
            if any(pattern.casefold() in normalized for pattern in patterns):
                selected = profile
                break
        if selected != self._active_profile:
            self._active_profile = selected
            logger.info("active mapping profile: %s", selected or "global")

    @property
    def active_profile(self) -> str | None:
        return self._active_profile

    def _binding(self, key: str) -> KeyBinding:
        base = self.config.bindings.get(key, KeyBinding())
        if not self._active_profile:
            return base
        overlay = self.config.profiles.get(self._active_profile, {}).get(key)
        if overlay is None:
            return base
        gestures = dict(base.gesture)
        gestures.update(overlay.gesture)
        layers = dict(base.layers)
        layers.update(overlay.layers)
        return KeyBinding(
            tap=overlay.tap if overlay.tap is not None else base.tap,
            hold=overlay.hold if overlay.hold is not None else base.hold,
            double=overlay.double if overlay.double is not None else base.double,
            gesture=gestures,
            layers=layers,
        )

    def _handle_down(self, key: str) -> None:
        state = self._states.setdefault(key, _KeyState())

        if key in {"up", "down", "left", "right"}:
            ok = self._states.get("ok")
            action = self._binding("ok").gesture.get(key)
            if ok and ok.phase == "down" and action:
                ok.sequence += 1
                self._cancel(ok.timer)
                ok.suppress_tap = True
                state.sequence += 1
                state.phase = "consumed"
                self._perform(action, key="ok", is_hold=False)
                logger.info("gesture ok+%s", key)
                return

        if state.phase == "waiting_double":
            state.sequence += 1
            self._cancel(state.timer)
            state.timer = None
            state.phase = "consumed"
            action = self._binding(key).double
            if action:
                self._perform(action, key=key, is_hold=False)
            logger.info("double %s", key)
            return

        state.sequence += 1
        state.phase = "down"
        state.hold_fired = False
        state.suppress_tap = False
        token = state.sequence
        self._cancel(state.timer)
        state.timer = self.loop.call_later(
            self.config.settings.hold_ms / 1000,
            self._hold_timer,
            key,
            token,
        )

    def _hold_timer(self, key: str, token: int) -> None:
        state = self._states.get(key)
        if not state or state.sequence != token or state.phase != "down":
            return
        state.timer = None
        state.hold_fired = True
        if key == "back" and self.effective_layer == 0:
            if self.config.settings.delete_all_on_hold:
                action = Action(
                    type="macro",
                    steps=(
                        Action(type="key_stroke", key="a", mods=("ctrl",)),
                        Action(type="key_stroke", key="backspace"),
                    ),
                )
            else:
                action = Action(type="key_stroke", key="backspace")
            self._perform(action, key=key, is_hold=True)
            logger.info("hold %s (protected base delete)", key)
            return
        action = self._binding(key).hold
        if action:
            self._perform(action, key=key, is_hold=True)
        logger.info("hold %s", key)

    def _handle_up(self, key: str) -> None:
        state = self._states.get(key)
        if state is None:
            return
        if state.phase == "consumed":
            state.sequence += 1
            state.phase = "idle"
            return
        if state.phase != "down":
            return

        state.sequence += 1
        self._cancel(state.timer)
        state.timer = None
        if state.hold_fired or state.suppress_tap:
            if self._momentary_owner == key:
                self._momentary_layer = None
                self._momentary_owner = None
                self._notify_layer()
            state.phase = "idle"
            return

        binding = self._binding(key)
        if binding.double and self.config.settings.double_ms > 0:
            state.phase = "waiting_double"
            token = state.sequence
            state.timer = self.loop.call_later(
                self.config.settings.double_ms / 1000,
                self._double_timer,
                key,
                token,
            )
        else:
            state.phase = "idle"
            self._fire_tap(key)

    def _release_filtered_key(self, key: str) -> None:
        """Clean a pre-overlay hold without firing its tap when UI captures key-up."""
        state = self._states.get(key)
        if state is None or state.phase not in {"down", "consumed"}:
            return
        state.sequence += 1
        self._cancel(state.timer)
        state.timer = None
        state.phase = "idle"
        state.hold_fired = False
        state.suppress_tap = False
        if self._momentary_owner == key:
            self._momentary_layer = None
            self._momentary_owner = None
            self._notify_layer()

    def _double_timer(self, key: str, token: int) -> None:
        state = self._states.get(key)
        if not state or state.sequence != token or state.phase != "waiting_double":
            return
        state.timer = None
        state.phase = "idle"
        self._fire_tap(key)

    def _fire_tap(self, key: str) -> None:
        binding = self._binding(key)
        action = binding.layers.get(self.effective_layer) if self.effective_layer else None
        self._perform(action or binding.tap, key=key, is_hold=False)
        logger.info("tap %s (layer %d)", key, self.effective_layer)

    def _perform(self, action: Action | None, *, key: str, is_hold: bool) -> None:
        if action is None or action.type == "none" or action.type == "voice":
            return
        if action.type == "layer_momentary":
            if is_hold:
                self._momentary_layer = int(action.value or 0)
                self._momentary_owner = key
                self._notify_layer()
            return
        if action.type == "layer_toggle":
            number = int(action.value or 0)
            self._locked_layer = 0 if self._locked_layer == number else number
            if self._locked_layer:
                self._restart_layer_idle()
            else:
                self._cancel(self._layer_timer)
                self._layer_timer = None
            self._notify_layer()
            return
        task = self.loop.create_task(self.run_action(action))
        self._tasks.add(task)
        task.add_done_callback(self._action_done)

    def _action_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error(
                "remote action failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _restart_layer_idle(self) -> None:
        self._cancel(self._layer_timer)
        if self.config.settings.layer_idle_ms:
            self._layer_timer = self.loop.call_later(
                self.config.settings.layer_idle_ms / 1000,
                self._expire_layer,
            )

    def _expire_layer(self) -> None:
        self._layer_timer = None
        if self._locked_layer:
            self._locked_layer = 0
            self._notify_layer()
            logger.info("locked layer expired")

    def _track_escape(self, is_down: bool) -> None:
        self._cancel(self._escape_timer)
        self._escape_timer = None
        if is_down:
            self._escape_timer = self.loop.call_later(
                self.config.settings.escape_hold_ms / 1000,
                self._escape,
            )

    def _escape(self) -> None:
        self._escape_timer = None
        self._locked_layer = 0
        self._momentary_layer = None
        self._momentary_owner = None
        menu = self._states.setdefault("menu", _KeyState())
        menu.sequence += 1
        menu.phase = "consumed"
        self._notify_layer(force=True)
        if self.on_escape:
            self.on_escape()
        logger.info("escape hatch: returned to base layer")

    def _notify_layer(self, *, force: bool = False) -> None:
        layer = self.effective_layer
        if self.on_layer and (force or layer != self._last_layer):
            self.on_layer(layer)
        self._last_layer = layer

    @staticmethod
    def _cancel(handle: asyncio.TimerHandle | None) -> None:
        if handle:
            handle.cancel()
