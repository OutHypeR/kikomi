"""The DAVE decrypt step, with a stand-in session (a real one needs a live call)."""
from types import SimpleNamespace

import davey
from discord.ext.voice_recv.rtp import OPUS_SILENCE

from kikomi.dave_recv import DaveVoiceRecvClient


class FakeSession:
    def __init__(self, fail=False, passthrough=False):
        self.fail, self.passthrough, self.calls = fail, passthrough, []

    def decrypt(self, user_id, media_type, payload):
        self.calls.append((user_id, media_type, payload))
        if self.fail:
            raise RuntimeError("no key")
        return b"opus:" + payload

    def can_passthrough(self, user_id):
        return self.passthrough


def client(session, version=1):
    c = object.__new__(DaveVoiceRecvClient)
    c._connection = SimpleNamespace(dave_session=session, dave_protocol_version=version)
    c._ssrc_to_id = {100: 555}
    return c


def test_unencrypted_call_passes_through():
    assert client(None, version=0)._dave_decrypt(100, b"abc") == b"abc"


def test_decrypts_with_the_senders_key():
    s = FakeSession()
    assert client(s)._dave_decrypt(100, b"abc") == b"opus:abc"
    assert s.calls == [(555, davey.MediaType.audio, b"abc")]


def test_silence_and_unknown_senders():
    s = FakeSession()
    c = client(s)
    assert c._dave_decrypt(100, OPUS_SILENCE) == OPUS_SILENCE
    assert c._dave_decrypt(999, b"abc") == OPUS_SILENCE
    assert s.calls == []


def test_failures_drop_frames_unless_in_passthrough():
    assert client(FakeSession(fail=True))._dave_decrypt(100, b"abc") == OPUS_SILENCE
    assert client(FakeSession(fail=True, passthrough=True))._dave_decrypt(100, b"abc") == b"abc"
