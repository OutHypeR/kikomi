import numpy as np

from kikomi import audio
from kikomi.listener import UtteranceCollector

FRAME = audio.BYTES_PER_MS * 20


def tone(loud: bool) -> bytes:
    level = 3000 if loud else 0
    return (np.ones(FRAME // 2, dtype=np.int16) * level).tobytes()


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_utterance_ends_after_silence():
    clock = Clock()
    c = UtteranceCollector(silence_ms=500, min_speech_ms=100, clock=clock)
    starts = []
    for _ in range(3):
        starts.append(c.feed(1, tone(False)))  # preroll
        clock.t += 0.02
    for _ in range(25):  # 0.5 s of speech
        starts.append(c.feed(1, tone(True)))
        clock.t += 0.02
    assert starts.count(True) == 1
    assert c.poll() == []  # still inside the silence window
    clock.t += 0.6
    [u] = c.poll()
    assert u.user_id == 1 and len(u.pcm) == FRAME * 28  # preroll + speech
    assert abs(u.started - 0.06) < 1e-9  # when the first loud chunk arrived
    assert c.poll() == []


def test_blips_are_ignored():
    clock = Clock()
    c = UtteranceCollector(silence_ms=300, min_speech_ms=300, clock=clock)
    for _ in range(5):
        c.feed(2, tone(True))
        clock.t += 0.02
    clock.t += 1
    assert c.poll() == []


def test_downsample_shape():
    pcm = tone(True) * 50  # one second
    out = audio.discord_to_whisper(pcm)
    assert out.dtype == np.float32 and len(out) == 16000
    assert abs(out[0] - 3000 / 32768) < 1e-6
