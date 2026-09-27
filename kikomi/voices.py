"""Finding a voice for the character.

The voice starts out as whatever the character file says. Server managers can
change it with /voice in Discord, which searches the list of voices with the
helpers here. The choice is saved with the server's other settings (see
servers.py), and /voice with reset turns it back to the character file's voice.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

_voice_list: Optional[List[Dict[str, str]]] = None  # fetched once, then remembered


async def all_voices() -> List[Dict[str, str]]:
    """Every Edge voice, as {"id", "gender", "language"}, e.g.
    {"id": "en-US-AriaNeural", "gender": "Female", "language": "English (United States)"}."""
    global _voice_list
    if _voice_list is None:
        import edge_tts

        _voice_list = [
            {
                "id": v["ShortName"],
                "gender": v.get("Gender", ""),
                # "Microsoft Aria Online (Natural) - English (United States)" -> "English (United States)"
                "language": v.get("FriendlyName", "").rsplit(" - ", 1)[-1] or v.get("Locale", ""),
            }
            for v in await edge_tts.list_voices()
        ]
    return _voice_list


def describe(v: Dict[str, str]) -> str:
    """A readable one-line description, e.g. "en-US-AriaNeural | Female | English (United States)"."""
    return f"{v['id']} | {v['gender']} | {v['language']}"


def search(voices: List[Dict[str, str]], query: str, limit: int = 25) -> List[Dict[str, str]]:
    """Voices matching every word typed, in any order.

    So "female british", "en-gb", "aria" and "japanese" all work.
    """
    words = query.lower().split()
    # Common ways people describe accents, mapped to how the voice list names them.
    aliases = {"british": "united kingdom", "uk": "united kingdom", "american": "united states",
               "us": "united states", "australian": "australia", "aussie": "australia"}
    words = [aliases.get(w, w) for w in words]

    def matches(v: Dict[str, str], word: str) -> bool:
        if "-" in word:  # a code like "en-gb"
            return word in v["id"].lower()
        # Each word has to match the start of a word in the description, so
        # "male" doesn't match "female" and "aria" doesn't match "Bulgarian".
        # Voice names are split up too: "AriaNeural" counts as "aria" and "neural".
        tokens = re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", describe(v)).lower())
        return all(any(t.startswith(part) for t in tokens) for part in word.split())

    found = [v for v in voices if all(matches(v, w) for w in words)]
    return found[:limit]


def parse_percent(value: Optional[int]) -> Optional[str]:
    """10 -> "+10%", -5 -> "-5%" (the format Edge expects for speed)."""
    return None if value is None else f"{value:+d}%"


def parse_hz(value: Optional[int]) -> Optional[str]:
    """15 -> "+15Hz" (the format Edge expects for pitch)."""
    return None if value is None else f"{value:+d}Hz"

