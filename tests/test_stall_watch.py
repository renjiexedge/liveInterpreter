import time

import numpy as np

from audio.player import is_silent
from engines.gemini_live import _Link


def test_is_silent():
    assert is_silent(b"")
    assert is_silent(np.zeros(480, dtype="<i2").tobytes())
    assert is_silent(np.full(480, -64, dtype="<i2").tobytes())
    assert not is_silent(np.full(480, 2000, dtype="<i2").tobytes())
    assert not is_silent(np.array([0, -32768], dtype="<i2"))  # int16 min doesn't overflow abs()


def test_stall_needs_enough_unanswered_speech(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    link = _Link("B", margin_s=5.0)
    link.heard("hi")
    now[0] += 30
    assert not link.stalled(20.0, 40)  # too little speech
    link.heard("x" * 40)
    assert link.stalled(20.0, 40)
    link.answered()
    assert not link.stalled(20.0, 40)
