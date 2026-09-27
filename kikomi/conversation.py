"""The bot's memory of the conversation on one server.

Several people can talk to the bot at once, so each time it replies, it sees
everything said since its last reply as one block of lines, like:

    Ana: hey nova
    Ben: ask her about the game

Everything is kept in memory only. It's gone when the bot restarts or
someone runs /reset, and nothing is ever written to disk.
"""

from __future__ import annotations

from typing import Dict, List


class Conversation:
    # In "wake" mode people chat without the bot answering. We keep the last 20
    # things said, so it has some context when someone does call its name.
    MAX_PENDING = 20

    def __init__(self, max_turns: int = 30) -> None:
        self.max_turns = max_turns  # how many back-and-forth messages to remember
        # The conversation so far, in the format the AI expects:
        # {"role": "user" or "assistant", "content": "..."}
        self.history: List[Dict[str, str]] = []
        self.pending: List[str] = []  # what's been said since the bot last replied

    def hear(self, speaker: str, text: str, addressed: bool = True) -> None:
        """Remember that someone said something.

        ``addressed`` is False for things said near the bot but not to it. They
        are marked "(overheard)" so the AI knows not to answer them later.
        """
        self.pending.append(f"{speaker}: {text}" if addressed else f"(overheard) {speaker}: {text}")
        del self.pending[:-self.MAX_PENDING]  # keep only the most recent lines

    def take_turn(self) -> List[Dict[str, str]]:
        """The bot is about to reply: add everything new to the history and
        return the conversation to send to the AI."""
        if self.pending:
            self.history.append({"role": "user", "content": "\n".join(self.pending)})
            self.pending = []
        self._trim()
        return list(self.history)

    def reply(self, text: str) -> None:
        """Remember what the bot said."""
        if text.strip():
            self.history.append({"role": "assistant", "content": text.strip()})
            self._trim()

    def reset(self) -> None:
        """Forget everything."""
        self.history.clear()
        self.pending.clear()

    def _trim(self) -> None:
        """Forget the oldest messages once there are too many.

        The AI requires the conversation to start with something a person said,
        not with the bot talking, so we also drop any leading bot message.
        """
        if len(self.history) > self.max_turns:
            self.history = self.history[-self.max_turns:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)
