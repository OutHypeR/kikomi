"""Keeps track of who has agreed to let the bot listen to them (/optin).

This is saved in data/consent.json as a list of Discord user IDs for each
server. Nothing else about anyone is stored.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Set


class ConsentStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        # Checked from the audio thread and changed from the bot's main loop, so guard it.
        self._lock = threading.Lock()
        self._data: Dict[str, Set[int]] = {}  # server ID -> IDs of people who opted in
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._data = {g: set(ids) for g, ids in raw.items()}

    def allowed(self, guild_id: int, user_id: int) -> bool:
        """Has this person opted in on this server?"""
        with self._lock:
            return user_id in self._data.get(str(guild_id), ())

    def set(self, guild_id: int, user_id: int, value: bool) -> None:
        """Opt someone in (True) or out (False), and save straight away."""
        with self._lock:
            ids = self._data.setdefault(str(guild_id), set())
            (ids.add if value else ids.discard)(user_id)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file first, then swap it in. That way a crash
            # halfway through writing can't leave a broken, half-written file.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({g: sorted(v) for g, v in self._data.items() if v}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
