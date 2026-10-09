"""Microphone capture: 16 kHz 16-bit mono PCM chunks pushed into a queue.

* While the assistant speaks, :class:`MuteFlag` is set and chunks are dropped so
  the assistant does not hear itself (unmute happens 200-300 ms after playback).
* If the device refuses 16 kHz, the stream opens at the device's own rate and
  every chunk is converted with :func:`audio.resample.resample_int16`.
* Chunks quieter than the noise gate are replaced by silence but still forwarded,
  so speech-to-text keeps receiving audio (keep-alive, end-of-speech timing).
* A watchdog thread notices when the device disappears (USB unplug, driver reset)
  and reopens it every ~2 s, reporting through ``on_error`` / ``on_recovered``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from audio.resample import resample_int16
from core.config import Settings
from core.interfaces import Health

logger = logging.getLogger(__name__)

NO_MIC_HINT = "No microphone found. Plug one in or choose another in Settings."


class MuteFlag:
    """Thread-safe "mic is muted" flag with a delayed unmute."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._muted = False
        self._until = 0.0

    def mute(self) -> None:
        """Mute now."""
        with self._lock:
            self._muted = True

    def unmute(self, delay_s: float = 0.0) -> None:
        """Unmute, optionally only after ``delay_s`` seconds."""
        with self._lock:
            self._muted = False
            self._until = self._clock() + delay_s

    @property
    def is_muted(self) -> bool:
        """True while muted or during the unmute delay."""
        with self._lock:
            return self._muted or self._clock() < self._until


def parse_device(value: str) -> int | str | None:
    """Config ``MIC_DEVICE``: empty = default device, digits = index, else a name."""
    value = value.strip()
    if not value:
        return None
    return int(value) if value.isdigit() else value


def level_of(chunk: bytes) -> float:
    """Loudness of a 16-bit PCM chunk from 0.0 to 1.0 (RMS), for the UI meter."""
    if len(chunk) < 2:
        return 0.0
    samples = np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype=np.int16)
    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
    return min(1.0, rms / 8000.0)


def _import_sounddevice() -> Any:
    """Import ``sounddevice``; a missing PortAudio DLL becomes a friendly RuntimeError."""
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            f"Microphone not available: the sound library could not load ({exc}). "
            "Reinstall the audio components (pip install sounddevice)."
        ) from exc
    return sounddevice


def _friendly(exc: BaseException) -> str:
    """A message for the UI explaining why the microphone could not open."""
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if any(k in low for k in ("invalid device", "no default", "unavailable", "-9996", "-9985")):
        hint = NO_MIC_HINT
    elif "sample rate" in low or "-9997" in low:
        hint = "The microphone does not support the needed sample rate."
    elif "in use" in low or "-9988" in low or "exclusive" in low:
        hint = "Another app is using the microphone."
    else:
        hint = "Could not open the microphone."
    return f"Microphone not available: {hint} ({text})"


def list_input_devices(backend: Any = None) -> list[tuple[int, str]]:
    """``(index, name)`` of every input device, for the Settings device picker.

    Returns an empty list when the sound library cannot load.
    """
    try:
        sd = backend or _import_sounddevice()
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001 - a broken audio stack just means "no devices"
        logger.warning("Could not list audio devices", exc_info=True)
        return []
    return [
        (index, str(dev.get("name", index)))
        for index, dev in enumerate(devices)
        if dev.get("max_input_channels", 0) > 0
    ]


class Microphone:
    """Captures audio with ``sounddevice`` into :attr:`chunks`.

    Args:
        sample_rate: output rate, 16000 (Sarvam realtime STT accepts 8000 or 16000 only).
        chunk_ms: chunk length; 100 ms = 3200 bytes at 16 kHz.
        mute: shared flag; chunks are dropped while it is set.
        device: index or name from ``MIC_DEVICE``; ``None`` = system default.
        on_level: optional callback with the 0-1 level of each chunk (UI meter).
        max_queue: chunks kept before new chunks are dropped (100 = 10 s).
        noise_gate: chunks with a level below this (0-1) are zeroed but still queued.
        on_error: called with a message when the device is lost or recovery gives up.
        on_recovered: called after a lost device was reopened.
        backend: a ``sounddevice``-like module (tests pass a fake).
        retry_interval_s: wait between reopen attempts after a failure.
        max_retries: reopen attempts before giving up (``on_error`` is called).
        stall_s: no audio callback for this long counts as a lost device.
        watch_interval_s: how often the watchdog checks the stream.
    """

    def __init__(
        self,
        sample_rate: int,
        chunk_ms: int,
        mute: MuteFlag,
        device: int | str | None = None,
        on_level: Callable[[float], None] | None = None,
        max_queue: int = 100,
        *,
        noise_gate: float = 0.0,
        on_error: Callable[[str], None] | None = None,
        on_recovered: Callable[[], None] | None = None,
        backend: Any = None,
        retry_interval_s: float = 2.0,
        max_retries: int = 30,
        stall_s: float = 2.0,
        watch_interval_s: float = 0.5,
    ) -> None:
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.blocksize = sample_rate * chunk_ms // 1000
        self.device_rate = sample_rate
        self.mute = mute
        self.device = device
        self.on_level = on_level
        self.noise_gate = noise_gate
        self.on_error = on_error
        self.on_recovered = on_recovered
        self.retry_interval_s = retry_interval_s
        self.max_retries = max_retries
        self.stall_s = stall_s
        self.watch_interval_s = watch_interval_s
        self.chunks: queue.Queue[bytes] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.error: str | None = None
        self._backend = backend
        self._stream: Any = None
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._last_callback = time.monotonic()

    @classmethod
    def from_settings(cls, settings: Settings, mute: MuteFlag, **kwargs: Any) -> Microphone:
        """Build from ``sample_rate``, ``chunk_ms``, ``mic_device`` and ``noise_gate``."""
        return cls(
            settings.sample_rate,
            settings.chunk_ms,
            mute,
            device=parse_device(settings.mic_device),
            noise_gate=settings.noise_gate,
            **kwargs,
        )

    # -- audio thread ------------------------------------------------------------
    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """sounddevice audio-thread callback: convert, measure, gate, queue."""
        self._last_callback = time.monotonic()
        if status:
            logger.debug("Mic status: %s", status)
        if self.mute.is_muted:
            return
        chunk = bytes(indata)
        if self.device_rate != self.sample_rate:
            chunk = resample_int16(chunk, self.device_rate, self.sample_rate)
        level = level_of(chunk)
        if self.on_level is not None:
            try:
                self.on_level(level)
            except Exception:  # noqa: BLE001 - a UI bug must not kill the audio thread
                logger.exception("Mic level callback failed")
        if level < self.noise_gate:
            chunk = bytes(len(chunk))
        try:
            self.chunks.put_nowait(chunk)
        except queue.Full:
            self.dropped += 1
            logger.warning("Mic queue full; dropping audio (%d chunks so far)", self.dropped)

    # -- opening and closing -------------------------------------------------------
    def _native_rate(self, sd: Any) -> int | None:
        """The device's default sample rate, or ``None`` if it cannot be queried."""
        try:
            info = sd.query_devices(self.device, "input")
            return int(info["default_samplerate"])
        except Exception:  # noqa: BLE001 - only a hint for the fallback rate
            return None

    def _make_stream(self, sd: Any, rate: int) -> Any:
        return sd.RawInputStream(
            samplerate=rate,
            blocksize=rate * self.chunk_ms // 1000,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=self._callback,
        )

    def _open(self) -> None:
        """Open and start the input stream. Raises ``RuntimeError`` with a friendly message."""
        sd = self._backend or _import_sounddevice()
        errors = (getattr(sd, "PortAudioError", OSError), ValueError, OSError)
        try:
            stream, rate = self._make_stream(sd, self.sample_rate), self.sample_rate
        except errors as first:
            native = self._native_rate(sd)
            if not native or native == self.sample_rate:
                raise RuntimeError(_friendly(first)) from first
            logger.info("Mic rejected %d Hz (%s); opening at %d Hz", self.sample_rate, first,
                        native)
            try:
                stream, rate = self._make_stream(sd, native), native
            except errors as exc:
                raise RuntimeError(_friendly(exc)) from exc
            # Warm up (imports scipy) here, not in the first audio callback.
            resample_int16(bytes(rate // 10 * 2), rate, self.sample_rate)
        try:
            stream.start()
        except errors as exc:
            _close_quietly(stream)
            raise RuntimeError(_friendly(exc)) from exc
        self.device_rate = rate
        self._last_callback = time.monotonic()
        self._stream = stream

    def start(self) -> None:
        """Open the input stream. Raises ``RuntimeError`` if no mic is available."""
        with self._lock:
            if self._stream is not None:
                return
            self._closing.clear()
            self._open()
            self.error = None
        self._watchdog = threading.Thread(target=self._watch, name="mic-watchdog", daemon=True)
        self._watchdog.start()
        logger.info("Microphone started (%d Hz -> %d Hz, device=%s)",
                    self.device_rate, self.sample_rate, self.device)

    def _close_stream(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            _close_quietly(stream)

    def stop(self) -> None:
        """Close the input stream, stop the watchdog and empty the queue."""
        self._closing.set()
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=3.0)
        self._close_stream()
        self.error = None
        self.drain()

    # -- recovery ----------------------------------------------------------------
    def _stream_ok(self) -> bool:
        """False when the stream died or no audio arrived for :attr:`stall_s`."""
        with self._lock:
            stream = self._stream
        if stream is None:
            return False
        active = bool(getattr(stream, "active", True))
        stalled = time.monotonic() - self._last_callback > self.stall_s
        return active and not stalled

    def _watch(self) -> None:
        """Watchdog thread: reopen the device when it disappears."""
        while not self._closing.wait(self.watch_interval_s):
            if self._stream_ok():
                continue
            if not self._recover():
                return

    def _reinit_backend(self) -> None:
        """Re-scan devices so a replugged mic is found (PortAudio caches the device list).

        Note: this also resets any open output stream, so it only runs after a plain
        reopen has already failed once.
        """
        sd = self._backend
        if sd is None:
            try:
                sd = _import_sounddevice()
            except RuntimeError:
                return
        terminate, initialize = getattr(sd, "_terminate", None), getattr(sd, "_initialize", None)
        if terminate is None or initialize is None:
            return
        try:
            terminate()
            initialize()
        except Exception:  # noqa: BLE001 - best effort; the next reopen will tell
            logger.debug("PortAudio re-initialise failed", exc_info=True)

    def _recover(self) -> bool:
        """Reopen the device every :attr:`retry_interval_s`. False if stopped or gave up."""
        if self._closing.is_set():
            return False
        self._report_error("Microphone disconnected. Reconnecting...")
        self._close_stream()
        last = ""
        for attempt in range(1, self.max_retries + 1):
            if self._closing.wait(self.retry_interval_s):
                return False
            if attempt > 1:
                self._reinit_backend()
            try:
                with self._lock:
                    if self._closing.is_set():
                        return False
                    self._open()
            except RuntimeError as exc:
                last = str(exc)
                logger.info("Mic reopen attempt %d/%d failed: %s", attempt, self.max_retries,
                            exc)
                continue
            self.error = None
            logger.info("Microphone recovered after %d attempt(s)", attempt)
            _notify(self.on_recovered)
            return True
        self._report_error(
            f"{last or 'Microphone lost.'} Gave up after {self.max_retries} tries; "
            "check the microphone and turn listening on again."
        )
        return False

    def _report_error(self, message: str) -> None:
        self.error = message
        logger.warning(message)
        callback = self.on_error
        if callback is not None:
            _notify(lambda: callback(message))

    # -- reading -----------------------------------------------------------------
    def drain(self) -> None:
        """Throw away queued audio (e.g. after the assistant spoke)."""
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                return

    def read(self, timeout: float = 0.5) -> bytes | None:
        """Next chunk, or ``None`` after ``timeout`` seconds."""
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def running(self) -> bool:
        """True while the stream is open."""
        return self._stream is not None

    def health(self) -> Health:
        """Status-bar health: red while the device is lost."""
        if self.error:
            return Health("audio", False, self.error)
        if self._stream is None:
            return Health("audio", True, "mic off")
        detail = f"mic on ({self.device_rate} Hz"
        if self.device_rate != self.sample_rate:
            detail += f" -> {self.sample_rate} Hz"
        detail += ")"
        if self.dropped:
            detail += f", {self.dropped} chunks dropped"
        return Health("audio", True, detail)


def _close_quietly(stream: Any) -> None:
    try:
        stream.stop()
        stream.close()
    except Exception:  # noqa: BLE001 - closing must not raise during shutdown or recovery
        logger.debug("Error closing microphone stream", exc_info=True)


def _notify(callback: Callable[[], Any] | None) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception:  # noqa: BLE001 - a listener bug must not stop recovery
        logger.exception("Microphone callback failed")
