"""Starts kikomi. Run it with:

    python -m kikomi

Options:
    --config path/to/file.yaml   use a different settings file (default: config.yaml)
    --debug                      show much more detail in the log
    --voices [words...]          list the voices you can use, e.g. --voices female british
    --stop                       ask the running bot to leave voice and shut down properly
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ROOT, load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="kikomi", description="kikomi: a Discord voice chat companion")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--stop", action="store_true",
                        help="ask the running bot to leave voice and shut down properly")
    parser.add_argument("--voices", nargs="*", metavar="WORD",
                        help='list the voices you can use, optionally searching, e.g. --voices female british')
    args = parser.parse_args()

    if args.stop:
        # The running bot checks for this file every second, then leaves voice
        # and shuts down. (Killing it instead leaves it showing in voice for ~30s.)
        from .bot import STOP_FILE

        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
        print("Asked the bot to stop. It will leave voice and shut down within a couple of seconds.")
        return

    if args.voices is not None:
        # Just list voices and stop; no Discord token needed.
        import asyncio

        from . import voices

        found = voices.search(asyncio.run(voices.all_voices()), " ".join(args.voices), limit=1000)
        for v in found:
            print(voices.describe(v))
        print(f"\n{len(found)} voices. Use one with /voice in Discord, or put it under voice: edge: in the character file.")
        return

    # Read the secret keys (Discord token, API keys) from the .env file.
    load_dotenv(ROOT / ".env")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # These libraries log a lot of routine detail; only show their warnings.
    for noisy in ("discord.gateway", "discord.voice_state", "discord.player", "discord.ext.voice_recv",
                  "httpx", "httpx2", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Lost voice packets are normal on any internet connection and make no audible
    # difference, but the voice library warns about every single one.
    logging.getLogger("discord.ext.voice_recv.opus").setLevel(logging.ERROR)

    # Check the two things people most often forget, with a clear message for each.
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not shutil.which("ffmpeg"):
        sys.exit("FFmpeg was not found on PATH. Install it so the bot can play audio (see README).")

    cfg = load_config(args.config)
    from .bot import KikomiBot  # imported here so the checks above run quickly

    KikomiBot(cfg).run(token, log_handler=None)  # log_handler=None: use the log setup above


if __name__ == "__main__":
    main()
