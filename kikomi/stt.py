"""Speech to text: turning what people say into written words.

This uses faster-whisper, a quick version of OpenAI's Whisper speech
recognition model. It runs on your own computer, on an NVIDIA graphics card
if you have one (much faster) or on the processor otherwise.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import List, Optional

import numpy as np

log = logging.getLogger(__name__)

# Whisper was trained partly on YouTube videos, so on silence or background
# noise it sometimes "hears" phrases like these that nobody said.
# If a whole transcript is one of these, we ignore it.
_HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "you",
    "bye",
    "subtitles by the amaraorg community",
    "please subscribe",
}


def _normalise(text: str) -> str:
    """Lowercase and strip punctuation, so "Thank you!" matches "thank you"."""
    return re.sub(r"[^\w\s]", "", text).strip().lower()


def _expose_pip_cuda_libs() -> None:
    """Help Windows find the NVIDIA graphics card libraries.

    Installing kikomi with the ``[gpu]`` option downloads NVIDIA's libraries
    as Python packages. Windows doesn't look inside Python's folders for them
    by default, so we point it there.
    """
    if os.name != "nt":
        return  # only needed on Windows
    try:
        import nvidia  # present only if the [gpu] extras are installed
    except ImportError:
        return
    for root in nvidia.__path__:
        for bin_dir in Path(root).glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


class Transcriber:
    """Turns audio into text."""

    def __init__(self, model: str = "small", device: str = "auto") -> None:
        self.model_name = model  # bigger models are more accurate but slower
        self.device = device  # "auto" tries the graphics card first, then falls back to the processor
        self._model = None  # loaded the first time it's needed
        # The model can only handle one piece of audio at a time, so we queue them up.
        self._lock = threading.Lock()

    def _load(self, device: str):
        """Load the Whisper model onto the graphics card if possible, otherwise the processor."""
        from faster_whisper import WhisperModel

        if device in ("auto", "cuda"):
            _expose_pip_cuda_libs()
            try:
                m = WhisperModel(self.model_name, device="cuda", compute_type="float16")
                log.info("Whisper '%s' loaded on GPU", self.model_name)
                return m
            except Exception as e:
                if device == "cuda":
                    raise  # the user asked for the graphics card specifically, so don't hide the error
                log.info("No usable GPU for Whisper (%s); using CPU", e)
        m = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        log.info("Whisper '%s' loaded on CPU", self.model_name)
        return m

    def warm_up(self) -> None:
        """Load the model ahead of time, so the first sentence isn't slow."""
        with self._lock:
            if self._model is None:
                self._model = self._load(self.device)

    def _pick_language(self, audio16k: np.ndarray, languages: Optional[List[str]]) -> Optional[str]:
        """Which language to hear this sentence as.

        Left to itself, Whisper guesses from any of ~99 languages, and on a
        short clip it often guesses wrong (a quick English "hey" can come out
        as Polish or French). So it only chooses from the server's languages:
        with one language there's nothing to guess, and with several it picks
        whichever of them the sentence sounds most like.
        """
        if not languages:
            return None  # no list at all: let Whisper choose from everything
        if len(languages) == 1:
            return languages[0]
        _, _, scores = self._model.detect_language(audio16k)
        allowed = [(lang, score) for lang, score in scores if lang in languages]
        return max(allowed, key=lambda pair: pair[1])[0] if allowed else languages[0]

    def _run(self, audio16k: np.ndarray, hotwords: Optional[str], languages: Optional[List[str]]) -> str:
        segments, _ = self._model.transcribe(
            audio16k,
            language=self._pick_language(audio16k, languages),
            # Words to listen out for, like the character's name. Without this,
            # a short "Hey Nova" can come out as "Hey Noga".
            hotwords=hotwords or None,
            beam_size=1,  # take the first good guess; faster, and plenty accurate for chat
            vad_filter=True,  # skip parts that don't contain speech
            condition_on_previous_text=False,  # treat each sentence on its own
        )
        # Drop pieces Whisper itself thinks are probably not speech.
        return " ".join(
            s.text.strip() for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)
        ).strip()

    def transcribe(self, audio16k: np.ndarray, hotwords: Optional[str] = None,
                   languages: Optional[List[str]] = None) -> str:
        """Turn one sentence of audio into text ("" if nothing was said).

        ``languages`` are the languages people speak on this server (language
        codes like "en" or "zh"); the sentence is only ever heard as one of them.

        This takes a moment and blocks while it works, so the bot runs it on a
        separate thread to stay responsive.
        """
        with self._lock:
            if self._model is None:
                self._model = self._load(self.device)
            try:
                text = self._run(audio16k, hotwords, languages)
            except RuntimeError as e:
                # A missing graphics card library only shows up the first time
                # we actually transcribe something. If that happens, switch to
                # the processor instead of failing.
                if self.device != "auto":
                    raise
                log.warning("GPU transcription failed (%s); switching Whisper to CPU", e)
                self.device = "cpu"
                self._model = self._load("cpu")
                text = self._run(audio16k, hotwords, languages)
        return "" if _normalise(text) in _HALLUCINATIONS else text


def known_languages() -> List[str]:
    """Every language code the speech recogniser understands (about 99)."""
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    return list(_LANGUAGE_CODES)
