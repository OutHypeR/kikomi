"""Text to speech: giving the character a voice, with feeling.

The voice comes from Microsoft Edge's online voices, through the free
``edge-tts`` package. It needs no graphics card, and there are hundreds of
voices to choose from; list them with ``edge-tts --list-voices``. Set
``tts.provider`` in config.yaml to ``none`` for text replies only.

How moods change the voice
--------------------------
The AI marks each sentence with a mood, like ``[happy]`` or ``[sad]``. Every
mood has a "tone": a few adjustments applied while the sentence plays.

* ``speed``: 1.1 is 10% faster (excited people talk faster; sad people slower)
* ``pitch``: in semitones; +1 is a little higher (surprise lifts the voice)
* ``warmth``: 0 to 1; softens the voice by adding body and easing off the
  hiss (gentle, sad and shy lines sound warmer)
* ``gain_db``: loudness; -3 is quieter (shy lines are softer)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

log = logging.getLogger(__name__)

# The tone for each mood when the character file doesn't set its own.
DEFAULT_MOODS: Dict[str, Dict[str, float]] = {
    "neutral": {},
    "happy": {"speed": 1.08, "pitch": 0.8, "warmth": 0.25},
    "excited": {"speed": 1.14, "pitch": 1.2, "warmth": 0.15},
    "smug": {"speed": 0.96, "pitch": -0.2, "warmth": 0.4},
    "annoyed": {"speed": 1.1, "pitch": 0.3, "warmth": 0.2},
    "surprised": {"speed": 1.12, "pitch": 1.0, "warmth": 0.2},
    "sad": {"speed": 0.86, "warmth": 0.7, "gain_db": -2.0},
    "shy": {"speed": 0.91, "pitch": 0.3, "warmth": 0.5, "gain_db": -3.0},
}


# Used when the chosen voice can't speak a sentence (usually because it's in
# another language). It can speak dozens of languages.
DEFAULT_BACKUP_VOICE = "en-US-EmmaMultilingualNeural"


@dataclass
class Clip:
    """A spoken sentence, ready to play."""

    audio: bytes  # the sound file (MP3)
    filters: str = ""  # FFmpeg audio filters that give it its mood, if any


def tone_filters(tone: Dict[str, float]) -> str:
    """Turn a mood's tone into FFmpeg audio filters, applied while it plays."""
    speed = float(tone.get("speed", 1.0) or 1.0)
    pitch = 2.0 ** (float(tone.get("pitch", 0.0) or 0.0) / 12.0)  # semitones -> frequency ratio
    warmth = float(tone.get("warmth", 0.0) or 0.0)
    gain = float(tone.get("gain_db", 0.0) or 0.0)
    parts = []
    if abs(pitch - 1.0) > 1e-3:
        # Raising the pitch this way also speeds the audio up by the same
        # amount, so the tempo step below slows it back down.
        parts += ["aresample=48000", f"asetrate={48000 * pitch:.0f}", "aresample=48000"]
    tempo = speed / pitch
    if abs(tempo - 1.0) > 1e-3:
        parts.append(f"atempo={tempo:.4f}")
    if warmth:
        # More body below 350 Hz, less hiss above 5 kHz.
        parts += [f"bass=g={3.0 * warmth:.2f}:f=350", f"treble=g={-3.5 * warmth:.2f}:f=5000"]
    if gain:
        parts.append(f"volume={gain:.1f}dB")
    return ",".join(parts)


class TTS(Protocol):
    """What a voice must be able to do: say a sentence in a mood."""

    async def synth(self, text: str, mood: str = "neutral") -> Optional[Clip]: ...


class EdgeTTS:
    """Microsoft Edge's online voices."""

    def __init__(self, voice: str, rate: str = "+0%", pitch: str = "+0Hz",
                 moods: Optional[Dict[str, Dict[str, Any]]] = None,
                 backup: Optional[str] = DEFAULT_BACKUP_VOICE) -> None:
        # rate: speed, e.g. "+10%" is a bit faster. pitch: e.g. "+20Hz" is a bit higher.
        self.voice, self.rate, self.pitch = voice, rate, pitch
        self.moods = moods or DEFAULT_MOODS
        # Most voices speak only their own language: an English voice produces
        # no sound at all for, say, a Chinese sentence. When that happens the
        # sentence is said by this backup voice instead, which speaks dozens of
        # languages, so the bot is never left silent.
        self.backup = backup

    async def _speak(self, text: str, voice: str) -> bytes:
        import edge_tts

        # The audio arrives in pieces; join them into one MP3.
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=self.rate, pitch=self.pitch).stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return bytes(audio)

    async def synth(self, text: str, mood: str = "neutral") -> Optional[Clip]:
        from edge_tts.exceptions import NoAudioReceived

        try:
            audio = await self._speak(text, self.voice)
        except NoAudioReceived:
            if not self.backup or self.backup == self.voice:
                raise
            log.info("%s can't say %r; using the backup voice %s", self.voice, text[:40], self.backup)
            audio = await self._speak(text, self.backup)
        return Clip(audio, tone_filters(self.moods.get(mood) or {})) if audio else None


class NoTTS:
    """No voice: the bot stays silent in voice chat."""

    async def synth(self, text: str, mood: str = "neutral") -> Optional[Clip]:
        return None


def make_tts(cfg: dict, voice: dict, moods: Optional[dict] = None) -> TTS:
    """Build the voice chosen in config.yaml, using the voice settings (and
    mood tones) from the character file."""
    provider = cfg.get("provider", "edge")
    if provider == "edge":
        return EdgeTTS(
            voice=voice.get("edge", "en-US-EmmaMultilingualNeural"),
            rate=voice.get("rate", "+0%"),
            pitch=voice.get("pitch", "+0Hz"),
            moods=moods,
            backup=voice.get("backup", DEFAULT_BACKUP_VOICE),
        )
    if provider == "none":
        return NoTTS()
    raise ValueError(f"Unknown tts.provider '{provider}' (use 'edge' or 'none')")
