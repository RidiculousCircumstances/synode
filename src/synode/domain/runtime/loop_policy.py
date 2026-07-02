from __future__ import annotations

from typing import Literal, cast

NativeLoopMode = Literal["strict", "guided", "autonomous"]

NATIVE_LOOP_MODES: frozenset[str] = frozenset({"strict", "guided", "autonomous"})
DEFAULT_NATIVE_LOOP_MODE: NativeLoopMode = "guided"


def normalize_native_loop_mode(value: object | None, *, default: NativeLoopMode = DEFAULT_NATIVE_LOOP_MODE) -> NativeLoopMode:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in NATIVE_LOOP_MODES:
        return cast(NativeLoopMode, text)
    raise ValueError(f"unknown native loop mode: {value}")
