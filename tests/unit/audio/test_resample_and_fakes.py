"""Tests for the resampler, the sample WAVs, FakeMicrophone and FakeSpeaker."""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from audio.fake import SAMPLES_DIR, FakeMicrophone, FakeSpeaker, read_wav
from audio.mic import MuteFlag
from audio.resample import resample_int16, to_mono_int16


def sine(rate: int, seconds: float, freq: float = 440.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def peak_hz(pcm: bytes, rate: int) -> float:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.fft.rfftfreq(samples.size, 1 / rate)[int(np.argmax(spectrum))])


# -- resampler -----------------------------------------------------------------------


@pytest.mark.parametrize("rate", [48000, 44100, 32000, 22050, 8000])
def test_resample_chunk_sizes_and_pitch(rate: int) -> None:
    chunk = sine(rate, 0.1)
    out = resample_int16(chunk, rate, 16000)
    assert len(out) == 3200  # 100 ms at 16 kHz, no dropped samples
    long = resample_int16(sine(rate, 1.0), rate, 16000)
    assert abs(peak_hz(long, 16000) - 440) < 5


def test_resample_identity_and_odd_bytes() -> None:
    assert resample_int16(b"\x01\x00\x02", 16000, 16000) == b"\x01\x00"
    assert resample_int16(b"", 48000, 16000) == b""
    with pytest.raises(ValueError):
        resample_int16(b"\x00\x00", 0, 16000)


def test_to_mono() -> None:
    stereo = np.array([100, 300, -100, -300], dtype=np.int16).tobytes()
    assert np.frombuffer(to_mono_int16(stereo, 2), dtype=np.int16).tolist() == [200, -200]
    assert to_mono_int16(b"\x01\x00\x02", 1) == b"\x01\x00"


# -- sample files ------------------------------------------------------------------------


def test_sample_wavs_are_16k_mono_16bit() -> None:
    files = sorted(SAMPLES_DIR.glob("*.wav"))
    assert len(files) >= 3
    for path in files:
        with wave.open(str(path), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)


def test_read_wav_converts_rate_and_channels(tmp_path: Path) -> None:
    path = tmp_path / "stereo8k.wav"
    mono = np.frombuffer(sine(8000, 0.5), dtype=np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(np.repeat(mono, 2).tobytes())
    pcm = read_wav(path, 16000)
    assert len(pcm) == 16000 * 2 // 2  # 0.5 s at 16 kHz


# -- FakeMicrophone ------------------------------------------------------------------------


def test_fake_mic_replays_files_in_100ms_chunks() -> None:
    levels: list[float] = []
    mic = FakeMicrophone(realtime=False, on_level=levels.append)
    mic.start()
    assert mic.finished.wait(2.0)
    chunks = []
    while (chunk := mic.read(0.05)) is not None:
        chunks.append(chunk)
    mic.stop()
    assert chunks and all(len(c) == 3200 for c in chunks)
    assert max(levels) > 0.1  # the tone and the speech-like bursts
    assert min(levels) == 0.0  # digital silence after the tone
    assert len(chunks) == mic.sent


def test_fake_mic_honours_mute() -> None:
    mute = MuteFlag()
    mute.mute()
    mic = FakeMicrophone(realtime=False, mute=mute)
    mic.start()
    assert mic.finished.wait(2.0)
    assert mic.read(0.05) is None
    mic.stop()


def test_fake_mic_noise_gate() -> None:
    mic = FakeMicrophone([SAMPLES_DIR / "03_room_noise.wav"], realtime=False, noise_gate=0.05)
    mic.start()
    assert mic.finished.wait(2.0)
    chunk = mic.read(0.05)
    mic.stop()
    assert chunk == bytes(3200)


def test_fake_mic_realtime_pacing_and_trailing_silence() -> None:
    mic = FakeMicrophone([SAMPLES_DIR / "01_tone_then_silence.wav"], realtime=True)
    started = time.monotonic()
    mic.start()
    got = [mic.read(1.0) for _ in range(4)]
    elapsed = time.monotonic() - started
    mic.stop()
    assert all(c is not None for c in got)
    assert elapsed >= 0.25  # ~100 ms per chunk, not as fast as possible
    assert not mic.running


def test_fake_mic_missing_files() -> None:
    with pytest.raises(RuntimeError):
        FakeMicrophone([Path("does/not/exist.wav")]).start()


# -- FakeSpeaker -----------------------------------------------------------------------------


def test_fake_speaker_records_and_signals() -> None:
    mute = MuteFlag()
    events: list[object] = []
    speaker = FakeSpeaker(24000, mute, unmute_delay_s=0.0,
                          on_start=lambda: events.append("start"), on_finish=events.append)
    assert speaker.play(iter([b"\x00\x00" * 2400, b"\x00\x00" * 2400])) is True
    assert speaker.seconds_played == pytest.approx(0.2)
    assert events == ["start", False]
    assert not mute.is_muted


def test_fake_speaker_realtime_stop_is_fast() -> None:
    speaker = FakeSpeaker(24000, realtime=True)

    def endless():  # type: ignore[no-untyped-def]
        while True:
            yield b"\x00\x00" * 2400

    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(speaker.play(endless())))
    thread.start()
    time.sleep(0.25)
    started = time.monotonic()
    speaker.stop()
    thread.join(1.0)
    assert result == [False]
    assert time.monotonic() - started < 0.1
    assert 0.15 < speaker.seconds_played < 0.5  # real-time pacing
    assert speaker.aborted == 1
