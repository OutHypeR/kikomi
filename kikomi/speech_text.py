"""Getting the AI's reply ready to be spoken aloud.

Two jobs:

1. The AI writes its reply a few words at a time. Rather than wait for the
   whole thing, we cut it into sentences as they arrive, so the bot can start
   speaking the first sentence while the rest is still being written.
2. Some things read badly out loud, like *actions in asterisks*, **bold
   markers**, links and emoji. We strip them out before speaking.
"""

from __future__ import annotations

import re
from typing import List

# A sentence is some text ending in . ! ? or …, optionally followed by a closing
# quote or bracket, then a space. Chinese and Japanese end sentences with
# 。！？ and put no space after them, so those count on their own. A line break
# also ends a sentence.
_SENTENCE_END = re.compile(
    r"(.+?(?:[.!?…]+[\"')\]]*(?=\s|$)|[。！？]+[」』”’）]*))\s*|(.+?)\n+", re.S
)
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0000FE0F\U0000200D]+"
)
# Actions like *waves* or *laughs softly*. The extra rules make sure **bold**
# text isn't mistaken for an action and deleted.
_ACTION = re.compile(r"(?<!\*)\*(?!\*)[^*\n]{1,80}(?<!\*)\*(?!\*)")
_URL = re.compile(r"https?://\S+")
_MARKDOWN = re.compile(r"[*_`#>|~]+")  # formatting symbols
# A mood tag the AI puts before a sentence, like [happy] or [sad].
_MOOD_TAG = re.compile(r"\[\s*([A-Za-z]+)\s*\]\s*")


def take_mood(sentence: str, moods, current: str) -> tuple[str, str]:
    """Pull the mood tags out of a sentence.

    Returns the sentence without its tags, plus the mood to say it in: the
    first recognised tag in the sentence, or ``current`` (the mood of the
    sentence before) when it has none. Only known mood names count; anything
    else in square brackets is left alone.
    """
    mood = None

    def remove(m: re.Match) -> str:
        nonlocal mood
        name = m.group(1).lower()
        if name not in moods:
            return m.group(0)
        mood = mood or name
        return ""

    text = _MOOD_TAG.sub(remove, sentence).strip()
    return text, mood or current


def strip_moods(text: str, moods) -> str:
    """The reply without any mood tags, for showing in text chat."""
    cleaned = _MOOD_TAG.sub(lambda m: "" if m.group(1).lower() in moods else m.group(0), text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


class SentenceSplitter:
    """Give it the reply bit by bit; it gives back complete sentences."""

    def __init__(self, min_chars: int = 12) -> None:
        self.buf = ""  # text received but not yet a complete sentence
        # A sentence too short to sound natural on its own (like "Oh.") waits
        # here and gets joined onto the next one.
        self.pending = ""
        self.min_chars = min_chars

    def feed(self, text: str) -> List[str]:
        """Add the next bit of the reply; returns any sentences now complete."""
        self.buf += text
        out: List[str] = []
        while True:
            m = _SENTENCE_END.match(self.buf)
            # If the "sentence" runs right to the end of what we have so far, it
            # might not be finished. "The version is 3." could become "3.5".
            if not m or m.end() == len(self.buf) and not self.buf[-1:].isspace():
                break
            sentence = (m.group(1) or m.group(2) or "").strip()
            self.buf = self.buf[m.end():]
            if not sentence:
                continue
            sentence = f"{self.pending} {sentence}".strip()
            if len(sentence) < self.min_chars:
                self.pending = sentence
            else:
                self.pending = ""
                out.append(sentence)
        return out

    def flush(self) -> List[str]:
        """The reply is finished: return whatever text is left over."""
        rest = f"{self.pending} {self.buf}".strip()
        self.buf = self.pending = ""
        return [rest] if rest else []


def clean_for_speech(text: str) -> str:
    """Remove the things that sound wrong when read aloud."""
    text = _ACTION.sub(" ", text)
    text = _URL.sub(" a link ", text)
    text = _EMOJI.sub(" ", text)
    text = _MARKDOWN.sub("", text)
    return re.sub(r"\s+", " ", text).strip()  # tidy up leftover double spaces
