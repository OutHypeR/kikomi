"""Plays the character's voice in the voice channel.

Sentences are spoken one after another, in the order they were queued. The
queue can be cleared at any moment, which is how the bot goes quiet when
someone talks over it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Callable, Optional, Tuple

import discord

from .tts import Clip

log = logging.getLogger(__name__)


class Speaker:
    def __init__(self, voice_client: Callable[[], Optional[discord.VoiceClient]], volume: float = 1.0) -> None:
        self._vc = voice_client  # a function returning the current voice connection
        self.volume = volume  # 1.0 is normal volume
        self._queue: "asyncio.Queue[Tuple[Clip, str]]" = asyncio.Queue()  # (clip, sentence) waiting to play
        self._task = asyncio.create_task(self._run())  # keeps playing whatever gets queued
        # Sentences that have started playing in the current reply. If the bot
        # is interrupted, this is what people actually heard.
        self.spoken: list[str] = []

    @property
    def busy(self) -> bool:
        """True while the bot is talking or has more lined up to say."""
        vc = self._vc()
        return not self._queue.empty() or bool(vc and vc.is_playing())

    def say(self, clip: Clip, text: str) -> None:
        """Add a spoken sentence to the end of the queue."""
        self._queue.put_nowait((clip, text))

    def stop(self) -> None:
        """Go quiet: throw away everything queued and cut off the current sentence."""
        while not self._queue.empty():
            self._queue.get_nowait()
        vc = self._vc()
        if vc and vc.is_playing():
            # Careful: on the listening voice client, stop() also stops *hearing*
            # people, which would leave the bot deaf after every interruption.
            # stop_playing() only cuts off the voice.
            getattr(vc, "stop_playing", vc.stop)()

    async def wait_idle(self) -> None:
        """Wait until the bot has finished talking."""
        while self.busy:
            await asyncio.sleep(0.1)

    async def _run(self) -> None:
        """Runs in the background for as long as the bot is in the channel,
        playing each queued sentence and waiting for it to finish."""
        loop = asyncio.get_running_loop()
        while True:
            clip, text = await self._queue.get()
            vc = self._vc()
            if not vc or not vc.is_connected():
                continue
            done = asyncio.Event()
            # The clip goes into a temporary file for FFmpeg to read. (Feeding it
            # through a pipe instead makes discord.py print a scary-looking
            # "broken pipe" error whenever a sentence is cut off mid-way.)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(clip.audio)
            path = f.name
            # FFmpeg converts the sound file into the format Discord plays, and
            # applies the mood's finishing touches (pitch, warmth...) on the way.
            # "-loglevel error" keeps FFmpeg quiet unless something actually goes wrong.
            options = "-loglevel error" + (f' -af "{clip.filters}"' if clip.filters else "")
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(path, stderr=None, options=options), volume=self.volume
            )

            # Discord calls this from another thread when the sentence ends,
            # so we pass the "done" signal back to our own loop safely.
            def after(error: Optional[Exception]) -> None:
                if error:
                    log.warning("Playback error: %s", error)
                loop.call_soon_threadsafe(done.set)

            try:
                vc.play(source, after=after)
                self.spoken.append(text)
                await done.wait()
            except discord.ClientException as e:
                log.warning("Could not play clip: %s", e)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass  # FFmpeg may still be letting go of it; the system clears temp files anyway

    def close(self) -> None:
        """Stop talking and shut down the background player."""
        self.stop()
        self._task.cancel()
