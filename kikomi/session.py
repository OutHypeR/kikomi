"""The bot's time in a voice channel on one server.

This is where everything comes together:
hear someone -> turn it into text -> decide whether to answer ->
ask the AI for a reply -> speak it sentence by sentence.

Each Discord server gets its own session, with its own conversation memory
and its own settings (languages, mode, character, voice), chosen with /setup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Dict, Optional

import discord

from . import audio
from .config import TEXT_RULES, Character, load_character, voice_rules
from .conversation import Conversation
from .dave_recv import DaveVoiceRecvClient
from .listener import KikomiSink, Utterance, UtteranceCollector
from .speaker import Speaker
from .servers import ServerSettings
from .tts import TTS, Clip, make_tts
from .speech_text import SentenceSplitter, clean_for_speech, strip_moods, take_mood

if TYPE_CHECKING:
    from .bot import KikomiBot

log = logging.getLogger(__name__)


class GuildSession:
    def __init__(self, bot: "KikomiBot", guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild  # the Discord server
        cfg = bot.cfg
        self.listen_cfg = cfg["listening"]
        self.settings: ServerSettings = bot.servers.get(guild.id)  # this server's own choices
        self.character: Character
        self.tts: TTS
        self._load_character()
        self.conversation = Conversation(max_turns=cfg["memory"]["max_turns"])
        self.vc: Optional[DaveVoiceRecvClient] = None  # the voice connection, while in a channel
        self.speaker: Optional[Speaker] = None
        self._reply_task: Optional[asyncio.Task] = None  # the reply currently being written/spoken
        self._reply_again = False  # someone spoke to the bot while it was already replying
        self._last_reply_at = 0.0  # when the bot last finished talking
        self._last_reply_to: set[int] = set()  # who the bot last answered
        self._names: Dict[int, str] = {}  # remembered display names, in case someone leaves
        self.listening = True  # /listen off pauses hearing without leaving

    # ---- joining and leaving ------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self.vc and self.vc.is_connected())

    async def join(self, channel: discord.VoiceChannel | discord.StageChannel, greet: bool = True,
                   invited_by: Optional[int] = None) -> None:
        """Join a voice channel and start listening (and say hello, unless ``greet`` is False).

        ``invited_by`` is whoever ran /join. The greeting counts as a reply to
        them, so they can answer it without saying the character's name.
        """
        if self.connected:
            if self.vc.channel.id == channel.id:
                return  # already there
            await self.leave()
        self.vc = await channel.connect(cls=DaveVoiceRecvClient, self_deaf=False)
        self.speaker = Speaker(lambda: self.vc, volume=self.bot.cfg["playback"]["volume"])
        loop = asyncio.get_running_loop()
        lc = self.listen_cfg
        collector = UtteranceCollector(
            silence_ms=lc["silence_ms"],
            min_speech_ms=lc["min_speech_ms"],
            max_utterance_s=lc["max_utterance_s"],
            volume_threshold=lc["volume_threshold"],
        )
        # The listener works on background threads. These two lines hand its
        # news ("someone started talking", "here's a finished sentence") back
        # to the bot's main loop, where the rest of the code runs.
        sink = KikomiSink(
            collector,
            allowed=self._allowed,
            on_speech_start=lambda uid: loop.call_soon_threadsafe(self._on_speech_start, uid),
            on_utterance=lambda u: asyncio.run_coroutine_threadsafe(self._on_utterance(u), loop),
        )
        self.vc.listen(sink)
        log.info("Joined %s / %s", self.guild.name, channel.name)
        if greet and self.character.greeting:
            await self.speak(self.character.greeting)
            if invited_by is not None:
                self._last_reply_to = {invited_by}
                self._last_reply_at = time.monotonic()

    async def rejoin(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Leave and join again with a fresh voice connection, keeping the
        conversation. Useful when the connection gets stuck."""
        await self.leave()
        await self.join(channel, greet=False)

    async def leave(self) -> None:
        """Stop talking and listening, and leave the voice channel."""
        if self._reply_task:
            self._reply_task.cancel()
        if self.speaker:
            self.speaker.close()
            self.speaker = None
        if self.vc:
            try:
                self.vc.stop_listening()
            except Exception:
                pass  # we're leaving anyway
            await self.vc.disconnect(force=True)
            self.vc = None

    # ---- this server's settings --------------------------------------------

    @property
    def mode(self) -> str:
        """Either "wake" (answer when the character's name is said) or "always" (answer everything)."""
        return self.settings.mode

    def _load_character(self) -> None:
        """Load this server's character, with the voice picked for it by /voice, if any."""
        self.character = load_character(self.settings.character)
        self.character.voice = {**self.character.voice, **self.settings.voices.get(self.character.key, {})}
        if self.settings.name:
            self.character = self.character.renamed(self.settings.name)
        self.tts = make_tts(self.bot.cfg["tts"], self.character.voice, self.character.moods)

    async def sync_nickname(self) -> Optional[str]:
        """Show the character's name (e.g. "Luna") as the bot's nickname on this
        server, so people see the name they chose. Returns a note for the person
        who changed it if the bot isn't allowed to, otherwise None."""
        me = getattr(self.guild, "me", None)
        if me is None or me.display_name == self.character.name:
            return None
        try:
            await me.edit(nick=self.character.name)
            log.info("[%s] Nickname is now %s", self.guild.name, self.character.name)
        except discord.Forbidden:
            return ("I couldn't change my nickname to match: give me the **Change Nickname** permission "
                    "(Server Settings \u2192 Roles), then set the name again.")
        except discord.HTTPException as e:
            log.warning("Couldn't change nickname on %s: %s", self.guild.name, e)
        return None

    def update(self, **changes) -> None:
        """Change some of this server's settings and save them. Switching
        character starts a fresh conversation."""
        new_character = "character" in changes and changes["character"] != self.settings.character
        for key, value in changes.items():
            setattr(self.settings, key, value)
        self.bot.servers.save(self.guild.id, self.settings)
        self._load_character()
        if new_character:
            self.conversation.reset()

    # ---- hearing ------------------------------------------------------------

    def _allowed(self, user_id: int) -> bool:
        """Should we listen to this person? Only if listening isn't paused, and
        they've opted in (unless opt-in is switched off in config.yaml)."""
        if not self.listening:
            return False
        if not self.bot.cfg["privacy"]["require_opt_in"]:
            return True
        return self.bot.consent.allowed(self.guild.id, user_id)

    def _name(self, user_id: int) -> str:
        """The person's server nickname, as the AI will see it."""
        member = self.guild.get_member(user_id)
        name = member.display_name if member else self._names.get(user_id, f"User {user_id}")
        self._names[user_id] = name
        return name

    def _on_speech_start(self, user_id: int) -> None:
        """Someone started talking. If they're talking over the bot, the bot
        stops and lets them finish, just like a person would."""
        if self.listen_cfg["interrupt"] and self.speaker and self.speaker.busy:
            # In wake mode only the person being answered can interrupt, so
            # other people chatting in the background don't cut the bot off.
            if self.mode == "always" or user_id in self._last_reply_to:
                log.info("Stopped talking: %s started speaking", self._name(user_id))
                self.stop_talking()

    def stop_talking(self) -> None:
        """Stop talking right away and drop the rest of the reply."""
        if self.speaker:
            self.speaker.stop()
        if self._reply_task and not self._reply_task.done():
            self._reply_task.cancel()

    async def _on_utterance(self, utterance: Utterance) -> None:
        """Someone finished a sentence: write it down, and reply if it's for us."""
        user_id, pcm = utterance.user_id, utterance.pcm
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        # Speech recognition takes a moment, so it runs on its own thread
        # while the bot keeps doing everything else.
        text = await loop.run_in_executor(
            self.bot.stt_executor, self.bot.transcriber.transcribe,
            audio.discord_to_whisper(pcm), self.character.name, self.settings.languages,
        )
        if not text:
            return  # it was just noise
        name = self._name(user_id)
        log.info("[%s] %s: %s (%.1fs of audio, %.2fs to transcribe)",
                 self.guild.name, name, text, len(pcm) / audio.BYTES_PER_MS / 1000, time.monotonic() - started)
        answer = self._should_answer(user_id, text, utterance.started)
        self.conversation.hear(name, text, addressed=answer)
        if answer:
            self._last_reply_to = {user_id}
            self._start_reply()
        else:
            log.info("Not answering %s: no wake word, and not a follow-up", name)

    def _should_answer(self, user_id: int, text: str, spoke_at: float) -> bool:
        """Decide whether this sentence is meant for the bot.

        ``spoke_at`` is when the person *started* the sentence. That's what
        counts for follow-ups: a long answer that started in time still counts,
        even though it finishes after the window closes.
        """
        if self.mode == "always":
            return True
        if self.character.addressed_in(text):
            return True  # they said the character's name
        # Right after answering someone, keep listening to them for a little
        # while (followup_seconds) without needing the name again. That way a
        # back-and-forth feels natural.
        recent = spoke_at - self._last_reply_at < self.listen_cfg["followup_seconds"]
        return recent and user_id in self._last_reply_to

    # ---- replying -----------------------------------------------------------

    def _start_reply(self) -> None:
        if self._reply_task and not self._reply_task.done():
            # Already replying: finish that first, then answer what was just said.
            self._reply_again = True
            return
        self._reply_task = asyncio.create_task(self._reply_loop())

    async def _reply_loop(self) -> None:
        while True:
            self._reply_again = False
            await self._reply_once()
            if not self._reply_again:
                break

    async def _reply_once(self) -> None:
        """Write one reply with the AI and speak it.

        Speed matters in a voice chat, so this overlaps the steps. As soon as
        the AI finishes its first sentence, we start turning it into speech and
        playing it, while the AI carries on writing the next sentence.
        """
        messages = self.conversation.take_turn()
        if not messages:
            return
        heard = messages[-1]["content"]
        splitter = SentenceSplitter()
        full: list[str] = []  # the whole reply, piece by piece
        synth_tasks: list[asyncio.Task] = []  # one per sentence being turned into speech
        mood = "neutral"  # the AI sets this with tags like [happy]; it lasts until the next tag
        if self.speaker:
            self.speaker.spoken = []

        async def speak_in_order(sentence: str, mood: str, previous: Optional[asyncio.Task]) -> None:
            clip = await self._synth(sentence, mood)
            # Several sentences are voiced at the same time, and a short one can
            # finish before a longer one ahead of it. Wait for the one before,
            # so they're played in the right order.
            if previous:
                await previous
            if clip and self.speaker:
                self.speaker.say(clip, sentence)

        def queue(sentence: str) -> None:
            nonlocal mood
            sentence, mood = take_mood(sentence, self.character.moods, mood)
            spoken = clean_for_speech(sentence)
            if spoken:
                prev = synth_tasks[-1] if synth_tasks else None
                synth_tasks.append(asyncio.create_task(speak_in_order(spoken, mood, prev)))

        try:
            async for chunk in self.bot.llm.stream(self.system_prompt(voice=True), messages):
                full.append(chunk)
                for sentence in splitter.feed(chunk):
                    queue(sentence)
            for sentence in splitter.flush():
                queue(sentence)
            if synth_tasks:
                await synth_tasks[-1]
        except asyncio.CancelledError:
            # Someone talked over the bot. Remember only what it actually got to
            # say, so the AI knows it was cut off.
            for t in synth_tasks:
                t.cancel()
            said = " ".join(self.speaker.spoken) if self.speaker else ""
            self.conversation.reply(f"{said} [cut off]" if said else "")
            raise
        except Exception:
            log.exception("Reply failed")
            return

        # The reply is remembered with its mood tags, so the AI keeps using them;
        # the tags are left out of what people see.
        reply = "".join(full).strip()
        log.info("[%s] %s: %s", self.guild.name, self.character.name, reply)
        self.conversation.reply(reply)
        self._last_reply_at = time.monotonic()
        await self._post_transcript(heard, strip_moods(reply, self.character.moods))
        if self.speaker:
            await self.speaker.wait_idle()
        # The follow-up window (see _should_answer) starts when the bot stops talking.
        self._last_reply_at = time.monotonic()

    async def _synth(self, text: str, mood: str = "neutral") -> Optional[Clip]:
        """Turn text into speech in the given mood, logging (not crashing) if the
        voice service fails."""
        try:
            return await self.tts.synth(text, mood)
        except Exception as e:
            log.warning("TTS failed for %r: %s", text[:40], e)
            return None

    async def speak(self, text: str, mood: str = "happy") -> None:
        """Say a fixed line, like the greeting."""
        clip = await self._synth(clean_for_speech(text), mood)
        if clip and self.speaker:
            self.speaker.say(clip, text)

    def system_prompt(self, voice: bool) -> str:
        """The instructions for the AI: the character's personality, plus rules
        for speaking out loud (voice) or writing (text chat)."""
        rules = voice_rules(self.character.moods) if voice else TEXT_RULES
        return f"{self.character.persona}\n\n{rules}"

    def transcript_destination(self):
        """Where transcripts go: the channel picked with /transcripts, or else
        the voice channel's own text chat. None if there's nowhere to post."""
        if self.settings.transcript_channel:
            channel = self.guild.get_channel(self.settings.transcript_channel)
            if channel:
                return channel
            log.warning("The transcript channel was deleted; posting in the voice channel's chat instead")
        return self.vc.channel if self.vc else None

    async def _post_transcript(self, heard: str, reply: str) -> None:
        """Post what the bot heard and said, so everyone can see exactly what it
        understood. It goes to the voice channel's text chat, or to the channel
        picked with /transcripts."""
        destination = self.transcript_destination()
        if not (self.settings.transcripts and destination and reply):
            return
        lines = "\n".join(f"> {line}" for line in heard.splitlines())
        try:
            await destination.send(
                f"{lines}\n**{self.character.name}:** {reply}"[:2000],  # Discord's message length limit
                allowed_mentions=discord.AllowedMentions.none(),  # never ping anyone
            )
        except discord.HTTPException as e:
            log.debug("Couldn't post transcript: %s", e)

    # ---- text chat ----------------------------------------------------------

    async def text_reply(self, author: str, text: str) -> str:
        """Reply to an @mention in a text channel. It shares the same memory as
        voice, so you can mix talking and typing."""
        self.conversation.hear(author, text)
        messages = self.conversation.take_turn()
        reply = "".join([c async for c in self.bot.llm.stream(self.system_prompt(voice=False), messages)]).strip()
        self.conversation.reply(reply)
        return strip_moods(reply, self.character.moods)
