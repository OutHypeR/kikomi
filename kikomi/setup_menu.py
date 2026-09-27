"""The /setup menu: dropdowns for a server's settings, and a Save button.

It's shown only to the person who ran /setup, and only they can use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

import discord

from .config import list_characters, load_character
from .naming import NameForm, default_name
from .servers import COMMON_LANGUAGES, language_name

if TYPE_CHECKING:
    from .session import GuildSession

INTRO = """\
**Set up kikomi for this server**
Choose below, then press **Save**. You can run /setup again any time to change things.

**Languages**: pick every language people speak here. Speech is only ever recognised as one of \
these, so short phrases aren't mistaken for another language. English only is the default.
**Mode**: when the bot answers in voice.
**Character**: who the bot plays. **Rename** gives it a different name on this server. \
Change the voice itself with /voice.
**Transcripts**: whether it posts what it heard and said. They go in the voice channel's text chat; \
send them to another channel with /transcripts."""


def transcripts_where(session: "GuildSession") -> str:
    """Where transcripts are going, in words."""
    s = session.settings
    if not s.transcripts:
        return "not posted"
    channel = session.guild.get_channel(s.transcript_channel) if s.transcript_channel else None
    return f"posted in {channel.mention}" if channel else "posted in the voice channel's chat"


def summary(session: "GuildSession") -> str:
    """This server's settings, in a few lines."""
    s = session.settings
    v = session.character.voice
    mode = "answers when its name is said" if s.mode == "wake" else "answers everything it hears"
    return "\n".join([
        f"**Languages:** {', '.join(language_name(code) for code in s.languages)}",
        f"**Mode:** {s.mode} ({mode})",
        f"**Name:** {session.character.name}"
        + (f" (the {default_name(session)} character)" if session.settings.name else ""),
        f"**Voice:** {v.get('edge')} (change it with /voice)",
        f"**Transcripts:** {transcripts_where(session)}",
    ])


class SetupMenu(discord.ui.View):
    def __init__(self, session: "GuildSession", owner_id: int) -> None:
        super().__init__(timeout=900)  # the menu stops working after 15 minutes
        self.session = session
        self.owner_id = owner_id
        s = session.settings
        self.languages: List[str] = list(s.languages)
        self.mode, self.character, self.transcripts = s.mode, s.character, s.transcripts

        # Offer the common languages, plus any already chosen in config.yaml that aren't among them.
        codes = list(COMMON_LANGUAGES) + [c for c in s.languages if c not in COMMON_LANGUAGES]
        codes = codes[:25]  # Discord's limit for one menu
        languages = discord.ui.Select(
            placeholder="Languages people speak here", min_values=1, max_values=len(codes), row=0,
            options=[discord.SelectOption(label=language_name(c), value=c, default=c in s.languages) for c in codes],
        )
        mode = discord.ui.Select(placeholder="When to answer", row=1, options=[
            discord.SelectOption(label="Wake: answer when its name is said", value="wake", default=s.mode == "wake"),
            discord.SelectOption(label="Always: answer everything it hears", value="always",
                                 default=s.mode == "always"),
        ])
        character = discord.ui.Select(placeholder="Character", row=2, options=[
            discord.SelectOption(label=load_character(key).name, value=key, default=key == s.character)
            for key in list_characters()[:25]
        ])
        transcripts = discord.ui.Select(placeholder="Transcripts", row=3, options=[
            discord.SelectOption(label="Post transcripts", value="on", default=s.transcripts),
            discord.SelectOption(label="Don't post transcripts", value="off", default=not s.transcripts),
        ])

        # Each dropdown just remembers the choice; nothing is saved until Save.
        async def pick_languages(interaction: discord.Interaction) -> None:
            self.languages = list(languages.values)
            await interaction.response.defer()

        async def pick_mode(interaction: discord.Interaction) -> None:
            self.mode = mode.values[0]
            await interaction.response.defer()

        async def pick_character(interaction: discord.Interaction) -> None:
            self.character = character.values[0]
            await interaction.response.defer()

        async def pick_transcripts(interaction: discord.Interaction) -> None:
            self.transcripts = transcripts.values[0] == "on"
            await interaction.response.defer()

        languages.callback, mode.callback = pick_languages, pick_mode
        character.callback, transcripts.callback = pick_character, pick_transcripts
        for item in (languages, mode, character, transcripts):
            self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the person who opened the menu can use it."""
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run /setup yourself to change the settings.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary, row=4)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Give the character a different name on this server (saved straight away)."""
        await interaction.response.send_modal(NameForm(self.session))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.session.update(languages=self.languages, mode=self.mode, character=self.character,
                            transcripts=self.transcripts, set_up=True)
        note = await self.session.sync_nickname()  # in case the character changed
        self.stop()
        text = f"**Saved.** kikomi's settings for this server:\n{summary(self.session)}"
        await interaction.response.edit_message(content=f"{text}\n{note}" if note else text, view=None)
