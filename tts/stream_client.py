"""Bulbul text-to-speech with streaming, sentence pipelining and instant cancel.

Uses ``POST /text-to-speech/stream`` with ``output_audio_codec=linear16`` so audio
chunks are raw 16-bit PCM that can be played as they arrive. The reply is cleaned
for speech (:mod:`tts.text_cleaner`) and split into sentences
(:mod:`tts.sentence_splitter`), one request each.

:meth:`SarvamTTS.stream` returns a :class:`SpeechStream`: a background thread
downloads the sentences one after another into a bounded queue, so sentence N+1
is being generated while sentence N plays. Closing or cancelling the stream stops
that thread promptly and closes the HTTP response. If streaming fails before any
audio arrives, the REST endpoint ``POST /text-to-speech`` is used for that
sentence instead. See docs/sarvam_api_notes.md, sections 4-5.
"""

from __future__ import annotations

import base64
import io
import logging
import queue
import struct
import threading
import wave
import weakref
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from core.config import Settings
from core.interfaces import Health
from language.detect import to_tts_language
from sarvam.client import SarvamClient, SarvamError
from tts.sentence_splitter import MAX_SENTENCE_CHARS, MIN_SENTENCE_CHARS, split_sentences
from tts.text_cleaner import clean_for_speech

__all__ = [
    "MAX_SENTENCE_CHARS",
    "MIN_SENTENCE_CHARS",
    "REST_PATH",
    "STREAM_PATH",
    "SarvamTTS",
    "SpeechStream",
    "split_sentences",
    "wav_data_offset",
    "wav_to_pcm",
]

logger = logging.getLogger(__name__)

STREAM_PATH = "/text-to-speech/stream"
REST_PATH = "/text-to-speech"
PREFETCH_CHUNKS = 64  # ~6 s of 24 kHz audio buffered ahead of playback
_POLL_S = 0.02


def wav_data_offset(buffer: bytes) -> int | None:
    """Return where PCM data starts in a WAV header, or ``None`` if more bytes are needed.

    Returns 0 when ``buffer`` is not a WAV file (already raw PCM).
    """
    if len(buffer) < 12:
        return None if b"RIFF".startswith(buffer[:4]) else 0
    if buffer[:4] != b"RIFF" or buffer[8:12] != b"WAVE":
        return 0
    pos = 12
    while pos + 8 <= len(buffer):
        chunk_id = buffer[pos : pos + 4]
        (size,) = struct.unpack("<I", buffer[pos + 4 : pos + 8])
        if chunk_id == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)
    return None


def _pcm_chunks(byte_iter: Iterator[bytes]) -> Iterator[bytes]:
    """Strip an optional WAV header and keep chunks aligned to 2-byte samples."""
    head = b""
    started = False
    carry = b""
    for chunk in byte_iter:
        if not chunk:
            continue
        if not started:
            head += chunk
            offset = wav_data_offset(head)
            if offset is None:
                continue
            chunk, started = head[offset:], True
        data = carry + chunk
        cut = len(data) - (len(data) % 2)
        carry = data[cut:]
        if cut:
            yield data[:cut]
    if not started and head:
        offset = wav_data_offset(head) or 0
        if len(head) - offset >= 2:
            yield head[offset : len(head) - ((len(head) - offset) % 2)]


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Decode a WAV file to (16-bit mono PCM, sample rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise SarvamError("Unexpected TTS WAV format (need 16-bit mono)")
        return wav.readframes(wav.getnframes()), wav.getframerate()


class _Failure:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_END = object()


class SpeechStream:
    """An iterator of PCM chunks filled by a background producer thread.

    The producer starts on the first ``next()`` and pushes chunks into a bounded
    queue with :meth:`put`. :meth:`cancel` (thread-safe) and :meth:`close` end the
    iteration at once, run the registered cancel hooks (e.g. closing the HTTP
    response) and make :meth:`put` return False so the producer stops.

    Args:
        produce: called on the producer thread with this stream.
        max_chunks: queue size, i.e. how far the producer may run ahead.
    """

    def __init__(self, produce: Callable[[SpeechStream], None], max_chunks: int = PREFETCH_CHUNKS):
        self._produce = produce
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_chunks)
        self._cancel = threading.Event()
        self._hooks: list[Callable[[], Any]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._done = False

    # -- producer side ---------------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        """True after :meth:`cancel` or :meth:`close`."""
        return self._cancel.is_set()

    def put(self, chunk: bytes) -> bool:
        """Queue one chunk, waiting while the queue is full. False once cancelled."""
        while not self._cancel.is_set():
            try:
                self._queue.put(chunk, timeout=_POLL_S * 2)
                return True
            except queue.Full:
                continue
        return False

    def add_cancel_hook(self, hook: Callable[[], Any]) -> None:
        """Run ``hook`` when the stream is cancelled (immediately if it already is)."""
        with self._lock:
            if not self._cancel.is_set():
                self._hooks.append(hook)
                return
        _quietly(hook)

    def remove_cancel_hook(self, hook: Callable[[], Any]) -> None:
        """Forget a hook added with :meth:`add_cancel_hook`."""
        with self._lock:
            if hook in self._hooks:
                self._hooks.remove(hook)

    def _run(self) -> None:
        try:
            self._produce(self)
        except BaseException as exc:  # noqa: BLE001 - handed to the consumer thread
            if not self._cancel.is_set():
                self._final(_Failure(exc))
            return
        self._final(_END)

    def _final(self, item: Any) -> None:
        while not self._cancel.is_set():
            try:
                self._queue.put(item, timeout=_POLL_S * 2)
                return
            except queue.Full:
                continue

    # -- consumer side ---------------------------------------------------------------
    def __iter__(self) -> SpeechStream:
        return self

    def __next__(self) -> bytes:
        if self._done:
            raise StopIteration
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="tts-prefetch", daemon=True)
            self._thread.start()
        while True:
            if self._cancel.is_set():
                self._done = True
                raise StopIteration
            try:
                item = self._queue.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            if item is _END:
                self._done = True
                raise StopIteration
            if isinstance(item, _Failure):
                self._done = True
                raise item.exc
            return item

    def cancel(self) -> None:
        """Stop now: end iteration, stop the producer, close its network response."""
        with self._lock:
            if self._cancel.is_set():
                return
            self._cancel.set()
            hooks, self._hooks = self._hooks, []
        for hook in hooks:
            _quietly(hook)

    def close(self) -> None:
        """Same as :meth:`cancel` (generator-style ``close``)."""
        self.cancel()
        self._done = True

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the producer thread; True when it has finished (or never started)."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def __del__(self) -> None:
        self._cancel.set()


def _quietly(hook: Callable[[], Any]) -> None:
    try:
        hook()
    except Exception:  # noqa: BLE001 - a cancel hook must never raise
        logger.debug("TTS cancel hook failed", exc_info=True)


class SarvamTTS:
    """Turns reply text into a stream of PCM chunks (16-bit mono, ``sample_rate`` Hz).

    Args:
        client: Sarvam HTTP client.
        settings: model, pace, sample rate and per-language voices (``voice_for``).
        prefetch_chunks: how many chunks may be downloaded ahead of playback.
        clean: run :func:`tts.text_cleaner.clean_for_speech` on every reply.
    """

    def __init__(
        self,
        client: SarvamClient,
        settings: Settings,
        *,
        prefetch_chunks: int = PREFETCH_CHUNKS,
        clean: bool = True,
    ) -> None:
        self.client = client
        self.settings = settings
        self.sample_rate = settings.tts_sample_rate
        self.prefetch_chunks = prefetch_chunks
        self.clean = clean
        self.last_error = ""
        self._streams: weakref.WeakSet[SpeechStream] = weakref.WeakSet()
        self._lock = threading.Lock()

    def speaker_for(self, language: str | None) -> str:
        """The configured voice for ``language`` (``settings.tts_voices``, else default)."""
        return self.settings.voice_for(to_tts_language(language))

    def payload(self, text: str, language: str | None) -> dict[str, Any]:
        """Request body shared by the streaming and REST endpoints."""
        return {
            "text": text,
            "language_code": to_tts_language(language),
            "speaker": self.speaker_for(language),
            "model": self.settings.tts_model,
            "pace": self.settings.tts_pace,
            "speech_sample_rate": self.sample_rate,
        }

    def stream(self, text: str, language: str | None) -> SpeechStream:
        """PCM chunks for ``text``; sentence N+1 downloads while sentence N plays.

        Iterating raises :class:`SarvamError` if a sentence fails on both endpoints.
        """
        if self.clean:
            text = clean_for_speech(text, language)
        sentences = split_sentences(text)
        stream = SpeechStream(
            lambda job: self._produce(job, sentences, language), self.prefetch_chunks
        )
        with self._lock:
            self._streams.add(stream)
        return stream

    def cancel(self) -> None:
        """Cancel every stream in progress (Interrupt)."""
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            stream.cancel()

    def _produce(self, job: SpeechStream, sentences: list[str], language: str | None) -> None:
        """Producer thread: speak each sentence in order into ``job``."""
        try:
            for sentence in sentences:
                if job.cancelled or not self._speak_sentence(job, sentence, language):
                    return
        except Exception as exc:
            if not job.cancelled:
                self.last_error = str(exc)
            raise
        self.last_error = ""

    def _speak_sentence(self, job: SpeechStream, sentence: str, language: str | None) -> bool:
        """Stream one sentence into ``job``; False once the job was cancelled."""
        produced = False
        try:
            body = {**self.payload(sentence, language), "output_audio_codec": "linear16"}
            with self.client.stream_post(STREAM_PATH, body) as response:
                job.add_cancel_hook(response.close)
                try:
                    for pcm in _pcm_chunks(response.iter_bytes()):
                        produced = True
                        if not job.put(pcm):
                            return False
                finally:
                    job.remove_cancel_hook(response.close)
            return not job.cancelled
        except (SarvamError, httpx.HTTPError) as exc:
            if job.cancelled:
                return False
            if produced:
                logger.error("TTS stream broke mid-sentence: %s", exc)
                return True
            logger.warning("TTS streaming failed (%s); using REST fallback", exc)
        return all(job.put(chunk) for chunk in self._rest_sentence(sentence, language))

    def _rest_sentence(self, sentence: str, language: str | None) -> Iterator[bytes]:
        body = {**self.payload(sentence, language), "output_audio_codec": "wav"}
        result = self.client.post_json(REST_PATH, body)
        audios = result.get("audios") or []
        if not audios:
            raise SarvamError("TTS returned no audio")
        pcm, rate = wav_to_pcm(base64.b64decode(audios[0]))
        if rate != self.sample_rate:
            logger.warning("TTS fallback returned %d Hz, expected %d Hz", rate, self.sample_rate)
        step = self.sample_rate // 10 * 2  # ~100 ms per chunk
        for start in range(0, len(pcm), step):
            yield pcm[start : start + step]

    def health(self) -> Health:
        """Red after the last reply failed on both endpoints."""
        if self.last_error:
            return Health("tts", False, self.last_error)
        return Health("tts", True, "ready")
