"""Small helpers for working with raw audio.

Discord gives us sound as raw 16-bit samples: 48,000 samples per second, in
stereo (left and right), delivered in 20 millisecond chunks. The speech
recogniser (Whisper) wants something different: 16,000 samples per second,
in mono, as decimal numbers between -1 and 1. These helpers convert between
the two and measure how loud a chunk is.
"""

from __future__ import annotations

import numpy as np

DISCORD_RATE = 48_000  # samples per second
DISCORD_CHANNELS = 2  # stereo
FRAME_MS = 20  # Discord sends audio in 20 ms chunks
# 48,000 samples/s x 2 channels x 2 bytes per sample = 192 bytes per millisecond
BYTES_PER_MS = DISCORD_RATE * DISCORD_CHANNELS * 2 // 1000


def frame_ms(pcm: bytes) -> float:
    """How many milliseconds of sound a chunk of Discord audio holds."""
    return len(pcm) / BYTES_PER_MS


def rms(pcm: bytes) -> float:
    """How loud a chunk of audio is, from 0 (silent) up to 32768 (as loud as possible).

    This is the "root mean square": the average strength of the sound wave.
    """
    if len(pcm) < 2:
        return 0.0
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples)))


def discord_to_whisper(pcm: bytes) -> np.ndarray:
    """Convert Discord audio into the format Whisper expects.

    Steps: turn stereo into mono by averaging left and right, then go from
    48,000 to 16,000 samples per second by averaging every three samples into
    one. That's a simple approach, but it's good enough for speech and saves
    us from needing an extra audio library.
    """
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 4], dtype=np.int16)
    mono = samples.reshape(-1, 2).astype(np.float32).mean(axis=1)
    mono = mono[: len(mono) - len(mono) % 3]  # drop leftovers so it divides evenly by 3
    return (mono.reshape(-1, 3).mean(axis=1) / 32768.0).astype(np.float32)
