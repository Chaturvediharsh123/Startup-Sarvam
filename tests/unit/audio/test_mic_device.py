"""Microphone device handling with a fake ``sounddevice``: errors, rates, gate, recovery."""

from __future__ import annotations

import struct
import sys
import threading
from typing import Any

import pytest

from audio.mic import Microphone, MuteFlag, list_input_devices
from core.config import Settings, load_settings


class FakePortAudioError(Exception):
    """Stands in for ``sounddevice.PortAudioError``."""


class FakeInputStream:
    def __init__(self, backend: FakeBackend, **kwargs: Any) -> None:
        self.backend = backend
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.active = False
        self.closed = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True

    def feed(self, pcm: bytes) -> None:
        self.callback(pcm, len(pcm) // 2, None, None)


class FakeBackend:
    """Just enough of the ``sounddevice`` module for :class:`Microphone`."""

    PortAudioError = FakePortAudioError

    def __init__(self, rates: tuple[int, ...] = (16000,), default_rate: int = 48000) -> None:
        self.rates = rates
        self.default_rate = default_rate
        self.open_failures = 0
        self.streams: list[FakeInputStream] = []
        self.reinits = 0

    def RawInputStream(self, **kwargs: Any) -> FakeInputStream:  # noqa: N802 - sounddevice API
        if self.open_failures:
            self.open_failures -= 1
            raise FakePortAudioError("Error querying device -1 (Invalid device)")
        if kwargs["samplerate"] not in self.rates:
            raise FakePortAudioError("Error opening RawInputStream: Invalid sample rate [-9997]")
        stream = FakeInputStream(self, **kwargs)
        self.streams.append(stream)
        return stream

    def query_devices(self, device: Any = None, kind: str | None = None) -> Any:
        if kind:
            return {"name": "Fake mic", "default_samplerate": float(self.default_rate)}
        return [
            {"name": "Fake mic", "max_input_channels": 1},
            {"name": "Fake speaker", "max_input_channels": 0},
        ]

    def _terminate(self) -> None:
        self.reinits += 1

    def _initialize(self) -> None:
        return None


def tone(samples: int, amplitude: int) -> bytes:
    return struct.pack(f"<{samples}h", *([amplitude, -amplitude] * (samples // 2)))


def fast_mic(backend: FakeBackend, **kwargs: Any) -> Microphone:
    options: dict[str, Any] = {"retry_interval_s": 0.01, "watch_interval_s": 0.01}
    options.update(kwargs)
    return Microphone(16000, 100, MuteFlag(), backend=backend, **options)


def test_portaudio_load_failure_is_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def find_spec(self, name: str, path: Any, target: Any = None) -> None:
            if name == "sounddevice":
                raise OSError("PortAudio library not found")

    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Broken(), *sys.meta_path])
    mic = Microphone(16000, 100, MuteFlag())
    with pytest.raises(RuntimeError, match="Microphone not available"):
        mic.start()
    assert not mic.running


def test_missing_device_is_friendly_runtime_error() -> None:
    backend = FakeBackend(default_rate=16000)
    backend.open_failures = 5
    with pytest.raises(RuntimeError, match="No microphone found"):
        fast_mic(backend).start()


def test_opens_at_native_rate_and_resamples() -> None:
    backend = FakeBackend(rates=(48000,), default_rate=48000)
    mic = fast_mic(backend)
    mic.start()
    try:
        assert mic.device_rate == 48000
        assert backend.streams[0].kwargs["blocksize"] == 4800
        backend.streams[0].feed(tone(4800, 3000))
        chunk = mic.read(0.5)
        assert chunk is not None and len(chunk) == 3200  # 100 ms at 16 kHz
        assert "48000 Hz -> 16000 Hz" in mic.health().detail
    finally:
        mic.stop()


def test_noise_gate_zeroes_quiet_chunks_but_forwards_them() -> None:
    backend = FakeBackend()
    levels: list[float] = []
    mic = fast_mic(backend, noise_gate=0.05, on_level=levels.append)
    mic.start()
    try:
        quiet, loud = tone(1600, 100), tone(1600, 4000)
        backend.streams[0].feed(quiet)
        backend.streams[0].feed(loud)
        assert mic.read(0.5) == bytes(3200)
        assert mic.read(0.5) == loud
        assert 0 < levels[0] < 0.05  # the meter still shows the real level
    finally:
        mic.stop()


def test_recovers_after_unplug() -> None:
    backend = FakeBackend()
    errors: list[str] = []
    recovered = threading.Event()
    mic = fast_mic(backend, on_error=errors.append, on_recovered=recovered.set)
    mic.start()
    try:
        backend.open_failures = 2  # device still gone for two attempts
        backend.streams[0].active = False  # "unplugged"
        assert recovered.wait(3.0)
        assert errors and "disconnected" in errors[0].lower()
        assert len(backend.streams) == 2 and backend.streams[0].closed
        assert backend.reinits >= 1  # device list rescanned so a replugged mic is found
        assert mic.health().ok
        backend.streams[1].feed(tone(1600, 4000))
        assert mic.read(0.5) is not None
    finally:
        mic.stop()


def test_stalled_stream_counts_as_lost() -> None:
    backend = FakeBackend()
    recovered = threading.Event()
    mic = fast_mic(backend, stall_s=0.05, on_recovered=recovered.set)
    mic.start()
    try:
        assert recovered.wait(3.0)  # no callbacks at all -> reopened
    finally:
        mic.stop()


def test_gives_up_after_bounded_retries() -> None:
    backend = FakeBackend(default_rate=16000)
    errors: list[str] = []
    gave_up = threading.Event()

    def on_error(message: str) -> None:
        errors.append(message)
        if "Gave up" in message:
            gave_up.set()

    mic = fast_mic(backend, on_error=on_error, max_retries=3)
    mic.start()
    try:
        backend.open_failures = 100
        backend.streams[0].active = False
        assert gave_up.wait(3.0)
        assert len(errors) == 2
        health = mic.health()
        assert not health.ok and "Gave up" in health.detail
    finally:
        mic.stop()
    assert mic.health().ok  # stop clears the error


def test_stop_ends_watchdog_thread() -> None:
    mic = fast_mic(FakeBackend())
    mic.start()
    mic.start()  # second start is a no-op
    mic.stop()
    assert not mic.running
    assert not any(t.name == "mic-watchdog" and t.is_alive() for t in threading.enumerate())


def test_list_input_devices() -> None:
    assert list_input_devices(FakeBackend()) == [(0, "Fake mic")]


def test_from_settings(tmp_path: Any) -> None:
    settings: Settings = load_settings(
        env_file=None,
        overrides={"SARVAM_API_KEY": "sk_test", "MIC_DEVICE": "2", "NOISE_GATE": "0.04"},
        settings_file=None,
    )
    mic = Microphone.from_settings(settings, MuteFlag())
    assert (mic.device, mic.noise_gate, mic.sample_rate, mic.blocksize) == (2, 0.04, 16000, 1600)
