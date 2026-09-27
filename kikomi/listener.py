"""Works out when each person starts and stops talking.

Discord sends a steady trickle of tiny 20 ms audio chunks for everyone who is
talking. This module collects each person's chunks separately and decides
when they have finished a sentence, so the whole sentence can be sent to
speech recognition in one go.

A sentence is treated as finished once that person has been quiet for a short
while (``silence_ms``, 0.8 seconds by default). Discord stops sending audio
when someone goes quiet, so a gap in their audio counts as silence too.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

from discord.ext import voice_recv

from . import audio

log = logging.getLogger(__name__)

# We keep the last 10 chunks (about 0.2 seconds) from before someone starts
# talking. Without this, the start of their first word would get cut off.
PREROLL_FRAMES = 10


@dataclass
class _Speaker:
    """Everything we're tracking for one person who is (or was) talking."""

    frames: List[bytes] = field(default_factory=list)  # audio of the sentence so far
    preroll: Deque[bytes] = field(default_factory=lambda: collections.deque(maxlen=PREROLL_FRAMES))
    voiced_ms: float = 0.0  # how much of it was actually loud enough to be speech
    total_ms: float = 0.0  # total length, including pauses
    started: float = 0.0  # when they started this sentence
    last_voice: float = 0.0  # when we last heard them say something
    announced: bool = False  # whether we've already reported "this person started talking"


@dataclass
class Utterance:
    """One finished sentence from one person."""

    user_id: int
    pcm: bytes  # the audio
    started: float  # when they started saying it (time.monotonic() seconds)


class UtteranceCollector:
    """Collects audio per person and cuts it into sentences.

    It can be used from several threads at once, which matters because
    Discord audio arrives on a different thread from the one checking for
    finished sentences.
    """

    def __init__(
        self,
        *,
        silence_ms: int = 800,
        min_speech_ms: int = 350,
        max_utterance_s: float = 30.0,
        volume_threshold: float = 400.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.silence_s = silence_ms / 1000  # quiet time that ends a sentence
        self.min_speech_ms = min_speech_ms  # anything shorter is a cough or a click, not speech
        self.max_ms = max_utterance_s * 1000  # cut off very long monologues
        self.threshold = volume_threshold  # how loud a chunk must be to count as speech
        self.clock = clock  # the tests swap in a fake clock
        self._speakers: Dict[int, _Speaker] = {}  # keyed by Discord user ID
        self._lock = threading.Lock()

    def feed(self, user_id: int, pcm: bytes) -> bool:
        """Add one chunk of someone's audio.

        Returns True at the moment we decide this person has started talking
        (they've said enough to count as real speech). The bot uses that to
        stop talking when someone talks over it.
        """
        now = self.clock()
        ms = audio.frame_ms(pcm)
        loud = audio.rms(pcm) >= self.threshold
        with self._lock:
            s = self._speakers.setdefault(user_id, _Speaker())
            # Not talking yet: just remember the last few chunks in case they're about to start.
            if not s.frames and not loud:
                s.preroll.append(pcm)
                return False
            # They just started: begin the sentence with those remembered chunks.
            if not s.frames:
                s.started = now
                s.frames.extend(s.preroll)
                s.total_ms = sum(audio.frame_ms(f) for f in s.preroll)
                s.preroll.clear()
            s.frames.append(pcm)
            s.total_ms += ms
            if loud:
                s.voiced_ms += ms
                s.last_voice = now
            if not s.announced and s.voiced_ms >= self.min_speech_ms:
                s.announced = True
                return True
            return False

    def poll(self) -> List[Utterance]:
        """Return every sentence that has finished since the last check."""
        now = self.clock()
        done: List[Utterance] = []
        with self._lock:
            for uid, s in self._speakers.items():
                if not s.frames:
                    continue  # not talking
                quiet = now - s.last_voice >= self.silence_s
                if not quiet and s.total_ms < self.max_ms:
                    continue  # still talking
                # Too short to be real speech? Throw it away instead of transcribing it.
                if s.voiced_ms >= self.min_speech_ms:
                    done.append(Utterance(uid, b"".join(s.frames), s.started))
                self._speakers[uid] = _Speaker()  # start fresh for their next sentence
        return done

    def drop(self, user_id: int) -> None:
        """Forget anything we were collecting for this person."""
        with self._lock:
            self._speakers.pop(user_id, None)


class KikomiSink(voice_recv.AudioSink):
    """The connection point between Discord's voice feed and kikomi.

    The voice library calls ``write`` for every chunk of audio it receives.
    We pass the chunk on to the collector, and a background thread checks
    ten times a second for finished sentences.

    The callbacks run on background threads, not the bot's main loop, so
    whoever creates this sink has to hand the work back to the main loop.
    """

    def __init__(
        self,
        collector: UtteranceCollector,
        *,
        allowed: Callable[[int], bool],
        on_speech_start: Callable[[int], None],
        on_utterance: Callable[[Utterance], None],
        poll_interval: float = 0.1,
    ) -> None:
        super().__init__()
        self.collector = collector
        self.allowed = allowed  # returns False for people who haven't opted in
        self.on_speech_start = on_speech_start  # called when someone starts talking
        self.on_utterance = on_utterance  # called with each finished sentence
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(poll_interval,), name="utterance-poller", daemon=True)
        self._thread.start()

    def wants_opus(self) -> bool:
        # False = give us decoded, raw audio rather than Discord's compressed format.
        return False

    def write(self, user: Optional[object], data: voice_recv.VoiceData) -> None:
        # Ignore other bots, audio we can't match to a person, and anyone who hasn't
        # opted in. Their audio is discarded right here and never stored.
        if user is None or getattr(user, "bot", False) or not data.pcm:
            return
        uid = user.id  # type: ignore[attr-defined]
        if not self.allowed(uid):
            return
        if self.collector.feed(uid, data.pcm):
            self._safe(self.on_speech_start, uid)

    def _run(self, interval: float) -> None:
        """Background thread: keep checking for finished sentences until stopped."""
        while not self._stop.wait(interval):
            for utterance in self.collector.poll():
                self._safe(self.on_utterance, utterance)

    @staticmethod
    def _safe(fn: Callable[..., None], *args: object) -> None:
        """Run a callback, logging any error instead of letting it kill the thread."""
        try:
            fn(*args)
        except Exception:
            log.exception("listener callback failed")

    def cleanup(self) -> None:
        """Called by the voice library when listening stops."""
        self._stop.set()
