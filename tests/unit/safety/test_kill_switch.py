"""Emergency stop with fake mouse positions (the real mouse is never read)."""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock

import pytest

from safety.kill_switch import KillSwitch, enable_failsafe

SIZE = (1920, 1080)


def make(positions: list[tuple[int, int]], fired: list[int]) -> KillSwitch:
    it = iter(positions)
    return KillSwitch(lambda: fired.append(1), position=lambda: next(it),
                      screen_size=lambda: SIZE, failsafe=False)


@pytest.mark.parametrize("pos", [(0, 0), (1919, 0), (0, 1079), (1919, 1079), (3, 4), (1915, 1076)])
def test_corners_trigger(pos: tuple[int, int]) -> None:
    fired: list[int] = []
    assert make([pos], fired).check() is True
    assert fired == [1]


@pytest.mark.parametrize("pos", [(960, 540), (0, 540), (960, 0), (10, 10), (1900, 1079)])
def test_non_corners_do_not_trigger(pos: tuple[int, int]) -> None:
    fired: list[int] = []
    assert make([pos], fired).check() is False
    assert fired == []


def test_fires_once_per_visit() -> None:
    fired: list[int] = []
    ks = make([(0, 0), (1, 1), (0, 0), (500, 500), (0, 0)], fired)
    results = [ks.check() for _ in range(5)]
    assert results == [True, False, False, False, True]
    assert ks.triggers == 2


def test_callback_error_and_bad_position_are_survived() -> None:
    ks = KillSwitch(MagicMock(side_effect=RuntimeError("x")), position=lambda: (0, 0),
                    screen_size=lambda: SIZE, failsafe=False)
    assert ks.check() is True
    broken = KillSwitch(lambda: None, position=MagicMock(side_effect=OSError("no display")),
                        screen_size=lambda: SIZE, failsafe=False)
    assert broken.check() is False
    assert not broken.health().ok


def test_thread_start_stop() -> None:
    hit = threading.Event()
    ks = KillSwitch(hit.set, poll_s=0.01, position=lambda: (0, 0), screen_size=lambda: SIZE,
                    failsafe=False)
    ks.start()
    try:
        assert hit.wait(2)
        assert ks.health().ok
    finally:
        ks.stop()
    assert not ks.health().ok


def test_enable_failsafe(fake_pyautogui: MagicMock) -> None:
    assert enable_failsafe() is True
    assert sys.modules["pyautogui"].FAILSAFE is True


def test_start_enables_failsafe(fake_pyautogui: MagicMock) -> None:
    ks = KillSwitch(lambda: None, poll_s=0.01, position=lambda: (500, 500),
                    screen_size=lambda: SIZE)
    ks.start()
    ks.stop()
    assert fake_pyautogui.FAILSAFE is True
