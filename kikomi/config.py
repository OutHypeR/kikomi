"""Reads the settings file (config.yaml) and the character files."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parent.parent  # the project folder

# Settings used when config.yaml doesn't mention them.
# config.example.yaml explains what each one does.
DEFAULTS: Dict[str, Any] = {
    "character": "nova",
    "llm": {"provider": "anthropic", "model": "claude-opus-5", "effort": "low", "fallbacks": True, "max_tokens": 4096},
    "stt": {"model": "small", "device": "auto", "languages": ["en"]},
    "tts": {"provider": "edge"},
    "listening": {
        "mode": "wake",
        "followup_seconds": 30,
        "silence_ms": 800,
        "min_speech_ms": 350,
        "max_utterance_s": 30,
        "volume_threshold": 400,
        "interrupt": True,
        "leave_when_alone": True,
    },
    "privacy": {"require_opt_in": True, "post_transcripts": True},
    "memory": {"max_turns": 30},
    "playback": {"volume": 1.0},
}


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Combine the defaults with the user's settings. The user's settings win,
    and anything they left out keeps its default, even inside a section."""
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path) -> Dict[str, Any]:
    """Read config.yaml. If it doesn't exist, all the defaults are used."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _merge(DEFAULTS, data or {})


@dataclass
class Character:
    """A personality the bot can play, loaded from characters/<name>.yaml."""

    key: str  # the file name, e.g. "nova"
    name: str  # the display name, e.g. "Nova"
    persona: str  # description of the personality, given to the AI
    wake_words: List[str] = field(default_factory=list)  # words that get the bot's attention
    greeting: str = ""  # said when joining a voice channel
    voice: Dict[str, Any] = field(default_factory=dict)  # voice settings for each speech engine
    # The moods the character can speak in, and how each one changes the voice
    # (see kikomi/tts.py). Empty means the built-in set.
    moods: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def addressed_in(self, text: str) -> bool:
        """Did someone say one of the character's wake words?

        Speech recognition often gets names slightly wrong, so this is forgiving:

        * a greeting run into the name still counts: up to three extra letters
          in front ("Hi Nova" written as "Inova", "Heynova");
        * a word one letter off counts, for names of four letters or more
          ("Noga", "Nora" and "Nava" all count for "Nova").

        Longer words don't count, so "supernova" and "novel" don't wake the bot.
        """
        wake = [w.lower() for w in (self.wake_words or [self.name])]
        for token in re.findall(r"[^\W\d_]+", text.lower()):
            for w in wake:
                if token.endswith(w) and len(token) - len(w) <= 3:
                    return True
                if len(w) >= 4 and _one_letter_off(token, w):
                    return True
        return False


    def renamed(self, new_name: str) -> "Character":
        """The same character under a name a server chose for it (see /setup).

        The new name replaces the old one in the personality and greeting, and
        becomes the wake word. For a name of several words, the first word is
        the wake word ("Luna Belle" answers to "Luna").
        """
        new_name = new_name.strip()
        if not new_name or new_name == self.name:
            return self
        swap = lambda text: re.sub(rf"\b{re.escape(self.name)}\b", new_name, text)
        return replace(
            self,
            name=new_name,
            wake_words=[new_name.split()[0].lower()],
            persona=f"{swap(self.persona)}\n\nYour name is {new_name}.",
            greeting=swap(self.greeting),
        )


def valid_name(name: str) -> bool:
    """A name people can say out loud: 2 to 20 characters of letters, with
    spaces, hyphens or apostrophes between words. Letters from any language count."""
    return bool(re.fullmatch(r"[^\W\d_]+(?:[ '\-][^\W\d_]+)*", name.strip())) and 2 <= len(name.strip()) <= 20


def _one_letter_off(a: str, b: str) -> bool:
    """True if a and b differ by exactly one letter changed, added or removed."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def characters_dir() -> Path:
    return ROOT / "characters"


def private_dir() -> Path:
    """characters/private/ holds your own additions that are never uploaded to
    GitHub (git ignores the folder). A file here with the same name as a
    character adds to or overrides that character's settings, for example to
    give it a voice you only have the rights to use yourself."""
    return characters_dir() / "private"


def list_characters() -> List[str]:
    """The names of all the character files, e.g. ["nova"]."""
    names = {p.stem for p in characters_dir().glob("*.yaml")}
    names |= {p.stem for p in private_dir().glob("*.yaml")}
    return sorted(names)


def load_character(key: str) -> Character:
    """Read characters/<key>.yaml, plus characters/private/<key>.yaml if there is one."""
    public, private = characters_dir() / f"{key}.yaml", private_dir() / f"{key}.yaml"
    if not public.exists() and not private.exists():
        raise FileNotFoundError(f"No character file {key}.yaml in {characters_dir()}")
    d: Dict[str, Any] = {}
    for path in (public, private):
        if path.exists():
            d = _merge(d, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    from .tts import DEFAULT_MOODS

    return Character(
        key=key,
        name=d.get("name", key.title()),
        persona=(d.get("persona") or "").strip(),
        wake_words=[str(w) for w in d.get("wake_words") or []],
        greeting=d.get("greeting", ""),
        voice=d.get("voice") or {},
        moods=d.get("moods") or DEFAULT_MOODS,
    )


# Instructions added after the character's personality when replying in voice.
# They're written to the AI, which is why they say "you".
_VOICE_RULES = """\
You are talking out loud in a Discord voice channel, possibly with several people at once.
Each user message holds what people said since your last reply, one line each, as "Name: words".
Lines starting with "(overheard)" were said near you but not to you. Treat them as background: \
don't answer them unless someone talking to you brings them up.
Their words come from speech recognition, so expect misheard words and missing punctuation; \
go with the most sensible reading instead of pointing out typos.

Your reply is read aloud by an expressive voice that follows your mood.
Begin every reply with a mood tag in square brackets, chosen from exactly this list: {tags}.
Example: [happy] There you are! When your mood clearly turns part-way through, put a new tag \
right before the sentence where it turns: [happy] You won! [surprised] Wait, first try?
Choose the mood fresh for every reply, and let it be real: warm when things are nice, \
teasing when you're playful, sad when something's sad, surprised only by genuine surprises.
Write with lively punctuation (question marks, exclamation marks, commas, ellipses); \
the voice reads punctuation as expression, so flat text sounds flat.
- Keep it short and conversational, usually one to three sentences.
- Plain spoken words only: no markdown, lists, emoji, code blocks or *actions*.
- Don't start with your own name or a "Name:" label.
- Address people by name when it helps make clear who you're answering.
Latency-sensitive; begin your visible answer immediately."""


def voice_rules(moods) -> str:
    return _VOICE_RULES.format(tags=" ".join(f"[{m}]" for m in moods))


# The same, for replies to @mentions in text chat.
TEXT_RULES = """\
This message came through Discord text chat rather than voice, so it is written, not spoken.
Keep the same personality. Short replies are still best, and light formatting is fine. \
You don't need mood tags here."""
