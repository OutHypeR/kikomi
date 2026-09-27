"""Choosing what the character is called on a server.

The first time the bot joins voice on a server, it asks what it should be
called. Someone who can manage the server can pick a name, or keep the
character's own name (e.g. Nova). If nobody answers, it keeps its own name.
The name can be changed later from the /setup menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

import discord

from .config import load_character, valid_name

if TYPE_CHECKING:
    from .session import GuildSession


def default_name(session: "GuildSession") -> str:
    """The character's own name, from its character file."""
    return load_character(session.settings.character).name


async def apply_name(session: "GuildSession", name: Optional[str]) -> Tuple[str, Optional[str]]:
    """Save the name for this server (None keeps the character's own name).

    The bot's nickname on the server changes to match, and if it's in voice it
    introduces itself. Returns the name now in use, plus a note if the
    nickname couldn't be changed."""
    old = session.character.name
    session.update(name=name or None, named=True)
    new = session.character.name
    problem = await session.sync_nickname()
    if new != old and session.connected:
        await session.speak(f"Okay! From now on, I'm {new}.", "happy")
    return new, problem


def with_note(text: str, note: Optional[str]) -> str:
    return f"{text}\n{note}" if note else text


class NameForm(discord.ui.Modal):
    """The small form that pops up to type a name."""

    def __init__(self, session: "GuildSession", after=None) -> None:
        super().__init__(title="What should the bot be called?", timeout=600)
        self.session = session
        self.after = after  # called with the new name once it's saved
        self.name_box = discord.ui.TextInput(
            label="Name (leave empty to keep the default)",
            placeholder=default_name(session),
            default=session.settings.name or "",
            required=False,
            max_length=20,
        )
        self.add_item(self.name_box)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.name_box.value.strip()
        if name and not valid_name(name):
            await interaction.response.send_message(
                "Names can only have letters, with spaces, hyphens or apostrophes between words "
                "(2 to 20 characters). Try again.", ephemeral=True)
            return
        new, note = await apply_name(self.session, name)
        await interaction.response.send_message(with_note(f"Done! I'm **{new}** on this server now.", note))
        if self.after:
            await self.after(new)


def can_manage(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


class NamePrompt(discord.ui.View):
    """"What should I be called?" with Choose a name / Keep the default buttons,
    posted the first time the bot joins voice on a server."""

    QUESTION = ("**What should I be called on this server?** Someone who can manage the server can "
                "choose a name, or keep **{default}**. (It can be changed later with /setup.)")

    def __init__(self, session: "GuildSession") -> None:
        super().__init__(timeout=600)  # after 10 minutes with no answer, keep the default name
        self.session = session
        self.message: Optional[discord.Message] = None  # set after posting, so the buttons can be removed
        self.keep.label = f"Keep {default_name(session)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only people who can manage the server can choose."""
        if not can_manage(interaction):
            await interaction.response.send_message(
                "Only someone who can manage this server can choose my name.", ephemeral=True)
            return False
        return True

    async def _finish(self, text: str) -> None:
        self.stop()
        if self.message:
            try:
                await self.message.edit(content=text, view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Choose a name", style=discord.ButtonStyle.primary)
    async def choose(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async def done(name: str) -> None:
            await self._finish(f"I'm **{name}** on this server.")

        await interaction.response.send_modal(NameForm(self.session, after=done))

    @discord.ui.button(label="Keep the default", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        name, note = await apply_name(self.session, None)
        await interaction.response.edit_message(
            content=with_note(f"I'll stay **{name}**. (Change it any time with /setup.)", note), view=None)
        self.stop()

    async def on_timeout(self) -> None:
        """Nobody answered: keep the character's own name, and don't ask again."""
        if not self.session.settings.named:
            name, note = await apply_name(self.session, None)
            await self._finish(with_note(
                f"Nobody picked a name, so I'll stay **{name}**. (Change it any time with /setup.)", note))
