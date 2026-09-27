"""Lets the bot hear people in Discord's end-to-end encrypted voice channels.

Background: since March 2026 all Discord voice calls use end-to-end
encryption, a system Discord calls DAVE. Incoming voice is wrapped in two
layers of encryption:

1. an outer layer between you and Discord's servers, and
2. an inner, end-to-end layer that only people in the call can unlock.

discord.py (the main Discord library) joins the encrypted call, but only uses
it to encrypt what the bot says. The add-on we use for hearing people
(``discord-ext-voice-recv``) only removes the outer layer. The audio then
fails to play because it's still locked by the inner one.

This file adds the missing step: after the outer layer comes off, we unlock
the inner layer with the key belonging to whoever was speaking.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from discord.ext import voice_recv
from discord.ext.voice_recv.reader import AudioReader
from discord.ext.voice_recv.rtp import OPUS_SILENCE

try:
    import davey  # the library that handles Discord's end-to-end encryption
except ImportError:
    # discord.py won't join a voice channel without it anyway, so this only
    # matters for the tests.
    davey = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class DaveVoiceRecvClient(voice_recv.VoiceRecvClient):
    """A voice connection that can hear people in end-to-end encrypted calls."""

    _last_warning: float = 0.0  # so we don't flood the log with the same warning

    def listen(self, sink: voice_recv.AudioSink, *, after: Optional[voice_recv.reader.AfterCB] = None) -> None:
        """Start hearing people. This is the add-on's own ``listen``, with our
        extra unlocking step slotted in before any audio starts flowing."""
        import discord

        if not self.is_connected():
            raise discord.ClientException("Not connected to voice.")
        if not isinstance(sink, voice_recv.AudioSink):
            raise TypeError(f"sink must be an AudioSink not {type(sink).__name__}")
        if self.is_listening():
            raise discord.ClientException("Already receiving audio.")

        reader = AudioReader(sink, self, after=after)
        remove_outer_layer = reader.decryptor.decrypt_rtp

        # Replace the add-on's decrypt step with one that removes both layers.
        # This runs on the background thread that receives network packets.
        def decrypt_rtp(packet):
            return self._dave_decrypt(packet.ssrc, remove_outer_layer(packet))

        reader.decryptor.decrypt_rtp = decrypt_rtp
        self._reader = reader
        reader.start()

    def _dave_decrypt(self, ssrc: int, payload: bytes) -> bytes:
        """Unlock the inner (end-to-end) layer of one chunk of someone's voice.

        ``ssrc`` is the number Discord uses to label each person's audio stream.
        If anything goes wrong we return a chunk of silence, because passing on
        scrambled audio would crash the audio decoder.
        """
        state = self._connection
        session = getattr(state, "dave_session", None)
        if davey is None or session is None or not getattr(state, "dave_protocol_version", 0):
            return payload  # this call isn't end-to-end encrypted, so nothing to do
        if payload == OPUS_SILENCE:
            return payload  # Discord sends silence unencrypted
        user_id = self._ssrc_to_id.get(ssrc)
        if user_id is None:
            # We don't know who this stream belongs to yet, so we don't know
            # whose key to use. Discord tells us within a moment.
            return OPUS_SILENCE
        try:
            return session.decrypt(user_id, davey.MediaType.audio, payload)
        except Exception as e:
            try:
                # While encryption is being switched on, some chunks arrive
                # unencrypted. The library tells us when that's expected.
                if session.can_passthrough(user_id):
                    return payload
            except Exception:
                pass
            if "Unencrypted" in str(e):
                # Discord clients now and then send a chunk without the inner
                # encryption. The encryption library rightly refuses those, and
                # skipping one 20 ms chunk makes no audible difference, so this
                # is normal and only worth a note in --debug mode.
                log.debug("Skipped an unencrypted voice chunk from user %s", user_id)
                return OPUS_SILENCE
            now = time.monotonic()
            if now - self._last_warning > 10:
                self._last_warning = now
                log.warning("Could not decrypt voice from user %s (%s); dropping frames", user_id, e)
            return OPUS_SILENCE
