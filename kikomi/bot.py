"""The Discord bot itself: logging in, slash commands, replying to @mentions,
and leaving voice when everyone else has gone."""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands

from . import voices
from .config import ROOT, list_characters
from .consent import ConsentStore
from .llm import make_llm
from .servers import ServerStore
from .session import GuildSession
from .naming import NamePrompt, default_name
from .setup_menu import INTRO, SetupMenu, summary, transcripts_where
from .stt import Transcriber

log = logging.getLogger(__name__)

# `python -m kikomi --stop` creates this file to ask the running bot to shut down properly.
STOP_FILE = ROOT / "data" / "stop"

# Posted when the bot joins a voice channel, so everyone knows what it does.
OPT_IN_NOTICE = (
    "I'm in the voice channel. I only listen to people who have run **/optin**; "
    "everyone else is ignored. Speech is turned into text to reply to you, and "
    "the audio itself is never saved. Run **/optout** at any time."
)
OPEN_NOTICE = (
    "I'm in the voice channel and listening to everyone here. Speech is turned "
    "into text to reply to you, and the audio itself is never saved."
)
# Posted on /join until someone has run /setup on this server.
FIRST_TIME_NOTICE = (
    "**First time here!** Someone who can manage this server should run **/setup** to choose "
    "which languages people speak here, when I answer, and who I am. Until then I only "
    "understand **English** and answer when my name is said."
)


# The list /help shows. Keep it in step with the commands below.
HELP_TEXT = """\
**Talking in voice**
/join: join your voice channel
/rejoin: leave and come straight back (use it if I stop hearing or speaking)
/leave: leave the voice channel
/stop: stop talking right now
/listen on|off: pause or resume listening without leaving

**Privacy**
/optin: let me hear you on this server
/optout: stop me hearing you
/reset: make me forget this server's conversation
/status: what I'm doing, and whether I can hear you

**For server managers** (and anyone they give access to)
/setup: choose languages, mode, character, name and transcripts for this server
/transcripts: choose where transcripts of what I hear and say are posted, or turn them off
/mode wake|always: answer only when my name is said, or answer everything
/character: switch character
/voice: change my voice (search as you type, e.g. "female british")
/say: make me say something out loud

You can also @mention me in any text channel to chat in text."""


class KikomiBot(discord.Client):
    def __init__(self, cfg: Dict[str, Any]) -> None:
        # The default permissions are enough: server info and who's in which
        # voice channel. We don't need to read every message, because Discord
        # always shows a bot the messages that @mention it.
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)  # holds the slash commands
        self.servers = ServerStore(ROOT / "data" / "servers.json", cfg)  # each server's /setup choices
        self.llm = make_llm(cfg["llm"])
        stt = cfg["stt"]
        self.transcriber = Transcriber(stt["model"], stt["device"])
        # A single background worker for speech recognition, shared by all servers.
        self.stt_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")
        self.consent = ConsentStore(ROOT / "data" / "consent.json")
        self.sessions: Dict[int, GuildSession] = {}  # one per server, keyed by server ID
        self._commands_synced = False  # slash commands are registered once, on the first login
        _register_commands(self)

    def session(self, guild: discord.Guild) -> GuildSession:
        """Get this server's session, creating it the first time."""
        if guild.id not in self.sessions:
            self.sessions[guild.id] = GuildSession(self, guild)
        return self.sessions[guild.id]

    async def setup_hook(self) -> None:
        """Runs once while the bot starts up, before it connects to Discord."""
        # Start loading the speech recognition model in the background, so the
        # first sentence isn't slow. The bot doesn't wait for it: logging in
        # (and leaving any leftover voice channel) comes first.
        asyncio.get_running_loop().run_in_executor(self.stt_executor, self.transcriber.warm_up)
        STOP_FILE.unlink(missing_ok=True)  # an old request from before this start doesn't count
        asyncio.get_running_loop().create_task(self._watch_for_stop())

    async def _watch_for_stop(self) -> None:
        """Shut down properly when `python -m kikomi --stop` asks (it creates
        data/stop). A proper shutdown leaves voice straight away; a killed
        bot stays showing in the voice channel until Discord times it out."""
        while not self.is_closed():
            if STOP_FILE.exists():
                STOP_FILE.unlink(missing_ok=True)
                log.info("Asked to stop: leaving voice and shutting down")
                await self.close()
                return
            await asyncio.sleep(1)

    async def on_ready(self) -> None:
        """Runs once connected to Discord (and again after any reconnect)."""
        log.info("Logged in as %s", self.user)
        await self.leave_leftover_channels()  # first, so a restart clears the old channel quickly
        if not self._commands_synced:
            self._commands_synced = True
            await self.sync_commands()

    async def sync_commands(self) -> None:
        """Tell Discord about our slash commands. Registering them on one test
        server is instant; registering them everywhere can take a few minutes."""
        dev_guild = os.environ.get("DEV_GUILD_ID")
        if dev_guild:
            guild = discord.Object(int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to test server %s", dev_guild)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (can take a few minutes to appear)")

    async def leave_leftover_channels(self) -> None:
        """Leave any voice channel Discord still shows the bot in from before
        a restart.

        If the bot was stopped suddenly (a crash, or the window being closed),
        it never got to say goodbye, so Discord keeps showing it in the channel
        for a while even though nothing is listening.

        This only helps when Discord reports that channel to the new run. While
        the old connection is still open on Discord's side, nothing the new run
        sends removes it: Discord drops it on its own after about 30 seconds.
        Shutting down properly (Ctrl+C, or `python -m kikomi --stop`) avoids
        the problem entirely.
        """
        for guild in self.guilds:
            me = guild.me
            channel = me.voice.channel if me and me.voice else None
            session = self.sessions.get(guild.id)
            if channel and not (session and session.connected):  # not a channel we joined this time
                log.info("Leaving %s / %s (left over from before the restart)", guild.name, channel.name)
                try:
                    await guild.change_voice_state(channel=None)
                except discord.HTTPException as e:
                    log.warning("Couldn't leave %s / %s: %s", guild.name, channel.name, e)

    async def close(self) -> None:
        """Shutting down (Ctrl+C, or the program being closed normally): leave
        every voice channel properly first, instead of leaving the bot showing
        in them."""
        for session in list(self.sessions.values()):
            if session.connected:
                try:
                    await session.leave()
                except Exception:
                    log.exception("Couldn't leave voice while shutting down")
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        """Reply in text when someone @mentions the bot."""
        if message.author.bot or not message.guild or self.user not in message.mentions:
            return
        # Remove the @mention itself, leaving just what they wrote.
        text = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        if not text:
            return
        session = self.session(message.guild)
        async with message.channel.typing():  # shows "kikomi is typing…"
            try:
                reply = await session.text_reply(message.author.display_name, text)
            except Exception:
                log.exception("Text reply failed")
                reply = "Sorry, my brain glitched. Try again in a moment?"
        if reply:
            await message.reply(reply[:2000], mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        """Called whenever someone joins, leaves or moves between voice channels.
        If everyone has left the bot's channel, the bot leaves too."""
        if not self.cfg["listening"]["leave_when_alone"]:
            return
        session = self.sessions.get(member.guild.id)
        if not session or not session.connected or before.channel != session.vc.channel:
            return  # not about the bot's channel
        if any(not m.bot for m in session.vc.channel.members):
            return  # people are still there
        await asyncio.sleep(30)  # give people a moment to come back
        if session.connected and not any(not m.bot for m in session.vc.channel.members):
            log.info("Everyone left %s; leaving too", session.vc.channel.name)
            await session.leave()


def _register_commands(bot: KikomiBot) -> None:
    """Set up the slash commands (/join, /leave, /optin and so on)."""
    tree = bot.tree

    def your_channel(interaction: discord.Interaction):
        """The voice channel the person running the command is in, if any."""
        voice = getattr(interaction.user, "voice", None)
        return voice.channel if voice else None

    def connected_session(interaction: discord.Interaction):
        """This server's session, if the bot is in a voice channel here."""
        session = bot.sessions.get(interaction.guild_id)
        return session if session and session.connected else None

    @tree.command(description="Join your voice channel")
    @app_commands.guild_only()
    async def join(interaction: discord.Interaction) -> None:
        channel = your_channel(interaction)
        if not channel:
            await interaction.response.send_message("Join a voice channel first, then run /join.", ephemeral=True)
            return
        # Joining can take a few seconds. This tells Discord we're working on
        # it, so the command doesn't time out.
        await interaction.response.defer(thinking=True)
        session = bot.session(interaction.guild)
        try:
            await session.join(channel, invited_by=interaction.user.id)
        except Exception as e:
            log.exception("Couldn't join voice")
            await interaction.followup.send(f"Couldn't join the voice channel: {e}")
            return
        notice = OPT_IN_NOTICE if bot.cfg["privacy"]["require_opt_in"] else OPEN_NOTICE
        mode = "say my name to talk to me" if session.mode == "wake" else "I'll answer anything I hear"
        text = f"{notice}\nMode: **{session.mode}** ({mode})."
        if not session.settings.set_up:
            text += f"\n\n{FIRST_TIME_NOTICE}"
        if session.settings.named:
            # Keep the nickname in step with the chosen name (e.g. if it was changed by hand).
            note = await session.sync_nickname()
            if note:
                text += f"\n\n{note}"
        await interaction.followup.send(text)
        # The first time on this server, ask what the bot should be called.
        if not session.settings.named:
            prompt = NamePrompt(session)
            prompt.message = await interaction.followup.send(
                NamePrompt.QUESTION.format(default=default_name(session)), view=prompt, wait=True)

    @tree.command(description="Leave and come straight back, e.g. if the bot stops hearing or speaking")
    @app_commands.guild_only()
    async def rejoin(interaction: discord.Interaction) -> None:
        session = bot.session(interaction.guild)
        # Your channel if you're in one, otherwise the one the bot was in.
        channel = your_channel(interaction) or (session.vc.channel if session.connected else None)
        if not channel:
            await interaction.response.send_message("Join a voice channel first, then run /rejoin.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            await session.rejoin(channel)
        except Exception as e:
            log.exception("Couldn't rejoin voice")
            await interaction.followup.send(f"Couldn't rejoin the voice channel: {e}")
            return
        await interaction.followup.send("Back! Fresh connection, same conversation.")

    @tree.command(description="Leave the voice channel")
    @app_commands.guild_only()
    async def leave(interaction: discord.Interaction) -> None:
        session = connected_session(interaction)
        if session:
            await session.leave()
            await interaction.response.send_message("Bye for now!")
            return
        # Not connected in this run of the bot, but Discord may still show it in a
        # channel from before a restart. Ask Discord to take it out either way.
        try:
            await interaction.guild.change_voice_state(channel=None)
        except discord.HTTPException as e:
            log.warning("Couldn't leave voice: %s", e)
        await interaction.response.send_message(
            "I'm not connected any more. If Discord still shows me in the channel, that's left over from "
            "the bot being stopped suddenly, and Discord clears it on its own within about 30 seconds.",
            ephemeral=True)

    @tree.command(description="Stop talking right now, without leaving")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction) -> None:
        session = connected_session(interaction)
        if not session:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return
        session.stop_talking()
        await interaction.response.send_message("Okay, I'll stop there.", ephemeral=True)

    @tree.command(description="Pause or resume listening, without leaving")
    @app_commands.guild_only()
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"),
                                 app_commands.Choice(name="off", value="off")])
    async def listen(interaction: discord.Interaction, state: app_commands.Choice[str]) -> None:
        session = bot.session(interaction.guild)
        session.listening = state.value == "on"
        if session.listening:
            await interaction.response.send_message("I'm listening again.")
        else:
            await interaction.response.send_message(
                "I've stopped listening. Nothing anyone says is heard until someone runs /listen on.")

    # "ephemeral" replies are only visible to the person who ran the command.

    @tree.command(description="Let the bot hear you in voice chat on this server")
    @app_commands.guild_only()
    async def optin(interaction: discord.Interaction) -> None:
        bot.consent.set(interaction.guild_id, interaction.user.id, True)
        await interaction.response.send_message(
            "Got it, I'll listen to you in voice chat on this server. What you say is turned into text "
            "to reply to you; the audio is never saved. Run /optout to stop.",
            ephemeral=True,
        )

    @tree.command(description="Stop the bot hearing you in voice chat on this server")
    @app_commands.guild_only()
    async def optout(interaction: discord.Interaction) -> None:
        bot.consent.set(interaction.guild_id, interaction.user.id, False)
        await interaction.response.send_message("Done, I won't listen to you any more.", ephemeral=True)

    @tree.command(description="Choose when the bot answers in voice")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)  # only server managers see this command
    @app_commands.describe(mode="wake: only when its name is said · always: answers everything it hears")
    @app_commands.choices(mode=[app_commands.Choice(name="wake", value="wake"), app_commands.Choice(name="always", value="always")])
    async def mode(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        bot.session(interaction.guild).update(mode=mode.value)
        await interaction.response.send_message(f"Mode set to **{mode.value}**.")

    @tree.command(description="Make the bot forget this server's conversation")
    @app_commands.guild_only()
    async def reset(interaction: discord.Interaction) -> None:
        bot.session(interaction.guild).conversation.reset()
        await interaction.response.send_message("Conversation cleared.")

    @tree.command(description="Switch character")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)  # only server managers see this command
    async def character(interaction: discord.Interaction, name: str) -> None:
        if name not in list_characters():
            await interaction.response.send_message(f"No character called `{name}`.", ephemeral=True)
            return
        session = bot.session(interaction.guild)  # only this server changes
        session.update(character=name)
        note = await session.sync_nickname()
        text = f"Now playing **{session.character.name}**. Conversation cleared."
        await interaction.response.send_message(f"{text}\n{note}" if note else text)
        if session.connected and session.character.greeting:
            await session.speak(session.character.greeting)

    @character.autocomplete("name")
    async def _character_names(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        """Suggest character names as the user types (Discord shows at most 25)."""
        return [app_commands.Choice(name=n, value=n) for n in list_characters() if current.lower() in n.lower()][:25]

    @tree.command(description="Change the character's voice")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)  # only server managers see this command
    @app_commands.describe(
        name="Start typing to search, e.g. \"female british\" or \"aria\"",
        speed="Faster or slower, in percent: 10 is a bit faster, -10 a bit slower (default 0)",
        pitch="Higher or lower: 15 is a bit higher, -15 a bit lower (default 0)",
        reset="Go back to the voice in the character file",
    )
    async def voice(interaction: discord.Interaction, name: Optional[str] = None,
                    speed: Optional[app_commands.Range[int, -50, 100]] = None,
                    pitch: Optional[app_commands.Range[int, -50, 50]] = None,
                    reset: bool = False) -> None:
        session = bot.session(interaction.guild)  # only this server changes
        key = session.character.key
        saved = dict(session.settings.voices)
        if reset:
            saved.pop(key, None)
        else:
            if name is None and speed is None and pitch is None:
                now = session.character.voice
                await interaction.response.send_message(
                    f"{session.character.name}'s voice is **{now.get('edge')}** "
                    f"(speed {now.get('rate', '+0%')}, pitch {now.get('pitch', '+0Hz')}). "
                    "Pick a new one with /voice name:, or go back to the original with /voice reset:True.",
                    ephemeral=True)
                return
            if name is not None and name not in {v["id"] for v in await voices.all_voices()}:
                await interaction.response.send_message(
                    f"There's no voice called `{name}`. Pick one from the list that appears as you type.",
                    ephemeral=True)
                return
            choice = dict(saved.get(key, {}))
            if name is not None:
                choice["edge"] = name
            if speed is not None:
                choice["rate"] = voices.parse_percent(speed)
            if pitch is not None:
                choice["pitch"] = voices.parse_hz(pitch)
            saved[key] = choice
        session.update(voices=saved)
        v = session.character.voice
        await interaction.response.send_message(
            f"{session.character.name} now speaks with **{v.get('edge')}** "
            f"(speed {v.get('rate', '+0%')}, pitch {v.get('pitch', '+0Hz')}).")
        # If the bot is in a voice channel on this server, let everyone hear it.
        if session.connected:
            await session.speak("Hi! This is my new voice. How do I sound?")

    @voice.autocomplete("name")
    async def _voice_names(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        """Search the voices as the user types (Discord shows at most 25)."""
        try:
            found = voices.search(await voices.all_voices(), current or "en-US")
        except Exception:
            return []  # the voice list couldn't be fetched; typing a voice name still works
        return [app_commands.Choice(name=voices.describe(v)[:100], value=v["id"]) for v in found]

    @tree.command(description="Make the bot say something out loud")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)  # only server managers see this command
    @app_commands.describe(text="What to say", mood="How to say it (default: neutral)")
    async def say(interaction: discord.Interaction, text: app_commands.Range[str, 1, 400],
                  mood: Optional[str] = None) -> None:
        session = connected_session(interaction)
        if not session:
            await interaction.response.send_message("I'm not in a voice channel. Run /join first.", ephemeral=True)
            return
        mood = (mood or "neutral").lower()
        if mood not in session.character.moods:
            await interaction.response.send_message(
                f"I don't have a `{mood}` mood. Try one of: {', '.join(session.character.moods)}.", ephemeral=True)
            return
        await interaction.response.send_message(f"Saying it ({mood}).", ephemeral=True)
        await session.speak(text, mood)

    @say.autocomplete("mood")
    async def _mood_names(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        """Suggest the character's moods as the user types."""
        moods = bot.session(interaction.guild).character.moods
        return [app_commands.Choice(name=m, value=m) for m in moods if current.lower() in m][:25]

    @tree.command(description="What the bot is doing, and whether it can hear you")
    @app_commands.guild_only()
    async def status(interaction: discord.Interaction) -> None:
        session = bot.session(interaction.guild)
        if bot.cfg["privacy"]["require_opt_in"]:
            if bot.consent.allowed(interaction.guild_id, interaction.user.id):
                you = "You've opted in, so I can hear you."
            else:
                you = "You haven't opted in, so I can't hear you. Run /optin if you'd like to talk."
        else:
            you = "Opt-in is switched off on this bot, so I hear everyone in the channel."
        lines = [
            summary(session),
            f"**Voice channel:** {session.vc.channel.mention if session.connected else 'not in one'}",
            f"**Listening:** {'on' if session.listening else 'paused (/listen on to resume)'}",
            f"**Remembers:** {len(session.conversation.history)} messages of this conversation (/reset to clear)",
            you,
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(description="Set the bot up for this server: languages, mode, character, transcripts")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)  # only server managers see this command
    async def setup(interaction: discord.Interaction) -> None:
        session = bot.session(interaction.guild)
        await interaction.response.send_message(
            f"{INTRO}\n\n**Right now:**\n{summary(session)}",
            view=SetupMenu(session, interaction.user.id), ephemeral=True)

    @tree.command(description="Choose where transcripts of what the bot hears and says are posted")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)  # only server managers see this command
    @app_commands.describe(to="Where to post them", channel="The channel, if you chose \"a channel I pick\"")
    @app_commands.choices(to=[
        app_commands.Choice(name="the voice channel's chat", value="voice"),
        app_commands.Choice(name="a channel I pick", value="channel"),
        app_commands.Choice(name="nowhere (turn them off)", value="off"),
    ])
    async def transcripts(interaction: discord.Interaction, to: app_commands.Choice[str],
                          channel: Optional[discord.TextChannel] = None) -> None:
        session = bot.session(interaction.guild)
        if to.value == "channel":
            if channel is None:
                await interaction.response.send_message(
                    "Pick the channel too, e.g. `/transcripts to:a channel I pick channel:#kikomi-logs`.",
                    ephemeral=True)
                return
            # Check the bot can actually post there, so transcripts don't silently vanish.
            me = interaction.guild.me
            perms = channel.permissions_for(me) if me else None
            if perms is not None and not (perms.view_channel and perms.send_messages):
                await interaction.response.send_message(
                    f"I can't post in {channel.mention}. Give me **View Channel** and **Send Messages** "
                    "there, then try again.", ephemeral=True)
                return
            session.update(transcripts=True, transcript_channel=channel.id)
        elif to.value == "voice":
            session.update(transcripts=True, transcript_channel=None)
        else:
            session.update(transcripts=False)
        await interaction.response.send_message(f"Transcripts: {transcripts_where(session)}.")

    @tree.command(description="List all of the bot's commands")
    async def help(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)
