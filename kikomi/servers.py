"""Settings that each Discord server chooses for itself.

One copy of kikomi can be in several servers, and each can set it up its own
way with /setup: which languages people speak there, when the bot answers,
which character it plays, its voice, and whether it posts transcripts. They're
saved in data/servers.json. A server that hasn't run /setup yet uses the
defaults from config.yaml.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# The languages offered in /setup (Discord menus hold at most 25 choices). The
# codes are the ones the speech recogniser uses. Any of its ~99 languages can
# also be set in config.yaml.
COMMON_LANGUAGES: Dict[str, str] = {
    "en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "es": "Spanish",
    "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian", "ru": "Russian",
    "ms": "Malay", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese", "tl": "Tagalog",
    "hi": "Hindi", "ta": "Tamil", "ar": "Arabic", "tr": "Turkish", "pl": "Polish",
    "nl": "Dutch", "sv": "Swedish", "uk": "Ukrainian", "el": "Greek", "he": "Hebrew",
}


def language_name(code: str) -> str:
    return COMMON_LANGUAGES.get(code, code)


@dataclass
class ServerSettings:
    languages: List[str]  # what people speak here; speech is only ever recognised as one of these
    mode: str  # "wake": answer when the name is said; "always": answer everything
    character: str  # which character file to play
    transcripts: bool  # post what the bot heard and said
    # Where transcripts go: a text channel's ID, or None for the voice channel's own chat.
    transcript_channel: Optional[int] = None
    voices: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # /voice choices, per character
    name: Optional[str] = None  # what the character is called here; None keeps its own name (e.g. Nova)
    named: bool = False  # has someone chosen (or skipped choosing) a name here yet?
    set_up: bool = False  # has someone run /setup here yet?


class ServerStore:
    """Every server's settings, saved in data/servers.json."""

    def __init__(self, path: Optional[Path], cfg: Dict[str, Any]) -> None:
        self.path = path  # None keeps everything in memory (used by the tests)
        self.cfg = cfg
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        if path and path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def defaults(self) -> ServerSettings:
        """What a server gets before anyone runs /setup: the config.yaml settings."""
        return ServerSettings(
            languages=list(self.cfg["stt"]["languages"]),
            mode=self.cfg["listening"]["mode"],
            character=self.cfg["character"],
            transcripts=self.cfg["privacy"]["post_transcripts"],
        )

    def get(self, guild_id: int) -> ServerSettings:
        with self._lock:
            saved = self._data.get(str(guild_id))
        settings = self.defaults()
        for key, value in (saved or {}).items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        return settings

    def save(self, guild_id: int, settings: ServerSettings) -> None:
        with self._lock:
            self._data[str(guild_id)] = asdict(settings)
            if not self.path:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write a temporary file first, then swap it in, so a crash can't leave a broken file.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
