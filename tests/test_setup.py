"""Per-server settings: languages, /setup, the first-time notice, and who can change things."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from kikomi.stt import Transcriber


class FakeWhisper:
    """Pretends to detect languages; scores are what the real model returns."""

    def __init__(self, scores):
        self.scores, self.detected = scores, 0

    def detect_language(self, audio):
        self.detected += 1
        return self.scores[0][0], self.scores[0][1], self.scores


def pick(languages, scores):
    t = Transcriber()
    t._model = FakeWhisper(scores)
    return t._pick_language(np.zeros(16000, dtype=np.float32), languages), t._model.detected


def test_language_is_only_chosen_from_the_servers_list():
    # From live tests: short English clips were heard as Polish, French and Japanese.
    polish_guess = [("pl", 0.41), ("en", 0.38), ("zh", 0.05), ("fr", 0.04)]
    assert pick(["en"], polish_guess) == ("en", 0)  # one language: nothing to guess
    assert pick(["en", "zh"], polish_guess) == ("en", 1)  # best of the allowed ones
    assert pick(["zh", "ja"], [("en", 0.9), ("ja", 0.06), ("zh", 0.03)]) == ("ja", 1)
    assert pick(None, polish_guess) == (None, 0)  # no list: Whisper chooses freely


@pytest.fixture
def bot(monkeypatch, tmp_path):
    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config
    from kikomi.servers import ServerStore

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    b = KikomiBot(load_config(ROOT / "config.example.yaml"))
    b.servers = ServerStore(tmp_path / "servers.json", b.cfg)
    return b


def guild(gid=1):
    return SimpleNamespace(id=gid, name=f"server{gid}", get_member=lambda uid: None)


def test_new_servers_default_to_english_only(bot):
    s = bot.session(guild())
    assert s.settings.languages == ["en"] and not s.settings.set_up


def test_setup_menu_saves_this_servers_choices(bot, tmp_path):
    from kikomi.servers import ServerStore
    from kikomi.setup_menu import SetupMenu

    async def run():
        session = bot.session(guild(1))
        menu = SetupMenu(session, owner_id=42)
        import discord

        by_name = {item.placeholder: item for item in menu.children if isinstance(item, discord.ui.Select)}
        languages, mode = by_name["Languages people speak here"], by_name["When to answer"]
        transcripts = by_name["Transcripts"]
        save = next(item for item in menu.children if isinstance(item, discord.ui.Button) and item.label == "Save")
        replies = []
        interaction = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(
            defer=lambda: asyncio.sleep(0),
            edit_message=lambda **kw: asyncio.sleep(0, replies.append(kw["content"]))))

        # Choosing in the dropdowns (Discord fills in .values before calling back)
        languages._values = ["en", "zh"]
        await languages.callback(interaction)
        mode._values = ["always"]
        await mode.callback(interaction)
        transcripts._values = ["off"]
        await transcripts.callback(interaction)
        assert session.settings.languages == ["en"]  # nothing saved until Save

        await save.callback(interaction)
        return session, replies

    session, replies = asyncio.run(run())
    saved = ServerStore(tmp_path / "servers.json", bot.cfg).get(1)
    assert saved.languages == ["en", "zh"] and saved.mode == "always" and not saved.transcripts and saved.set_up
    assert session.mode == "always" and "English, Chinese" in replies[-1]
    assert bot.session(guild(2)).settings.languages == ["en"]  # other servers unaffected


def test_only_the_person_who_opened_setup_can_use_it(bot):
    from kikomi.setup_menu import SetupMenu

    async def run():
        menu = SetupMenu(bot.session(guild()), owner_id=42)
        told = []
        someone_else = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(
            send_message=lambda text, **kw: asyncio.sleep(0, told.append(text))))
        return await menu.interaction_check(someone_else), told

    allowed, told = asyncio.run(run())
    assert not allowed and "Run /setup yourself" in told[0]


def test_join_mentions_setup_until_it_has_been_done(bot, monkeypatch):
    from kikomi.bot import FIRST_TIME_NOTICE

    g = guild()
    session = bot.session(g)

    async def fake_join(channel, invited_by=None):
        pass

    monkeypatch.setattr(session, "join", fake_join)
    join = next(c for c in bot.tree.get_commands() if c.name == "join")
    sent = []
    interaction = SimpleNamespace(
        guild=g, guild_id=g.id, user=SimpleNamespace(id=5, voice=SimpleNamespace(channel=object())),
        response=SimpleNamespace(defer=lambda **kw: asyncio.sleep(0)),
        followup=SimpleNamespace(send=lambda text, **kw: asyncio.sleep(0, sent.append(text))))

    asyncio.run(join.callback(interaction))
    assert any(FIRST_TIME_NOTICE in text for text in sent)
    session.update(set_up=True)
    sent.clear()
    asyncio.run(join.callback(interaction))
    assert not any(FIRST_TIME_NOTICE in text for text in sent)


def test_settings_commands_are_for_server_managers_only(bot):
    managers_only = {c.name for c in bot.tree.get_commands()
                     if c.default_permissions is not None and c.default_permissions.manage_guild}
    assert managers_only == {"setup", "mode", "character", "voice", "say", "transcripts"}


def test_transcripts_can_go_to_a_chosen_channel(bot):
    posted = {"voice": [], "logs": []}

    def channel(name, can_post=True):
        async def send(text, **kw):
            posted[name].append(text)
        perms = SimpleNamespace(view_channel=True, send_messages=can_post)
        return SimpleNamespace(id=hash(name) % 1000, mention=f"#{name}", send=send,
                               permissions_for=lambda member: perms)

    voice_chat, logs, locked = channel("voice"), channel("logs"), channel("locked", can_post=False)
    g = SimpleNamespace(id=1, name="server1", get_member=lambda uid: None, me=object(),
                        get_channel=lambda cid: {logs.id: logs, locked.id: locked}.get(cid))
    session = bot.session(g)
    session.vc = SimpleNamespace(channel=voice_chat, is_connected=lambda: True)
    command = next(c for c in bot.tree.get_commands() if c.name == "transcripts")
    replies = []
    interaction = SimpleNamespace(guild=g, guild_id=1, response=SimpleNamespace(
        send_message=lambda text, **kw: asyncio.sleep(0, replies.append(text))))

    def to(value):
        return SimpleNamespace(value=value)

    asyncio.run(session._post_transcript("OutHype: hi nova", "Hey!"))
    assert len(posted["voice"]) == 1  # by default: the voice channel's own chat

    asyncio.run(command.callback(interaction, to("channel"), logs))
    asyncio.run(session._post_transcript("OutHype: hi nova", "Hey!"))
    assert len(posted["logs"]) == 1 and len(posted["voice"]) == 1 and "#logs" in replies[-1]

    asyncio.run(command.callback(interaction, to("channel"), locked))  # the bot can't post there
    assert "can't post in #locked" in replies[-1] and session.settings.transcript_channel == logs.id

    asyncio.run(command.callback(interaction, to("channel"), None))  # forgot to pick one
    assert "Pick the channel too" in replies[-1]

    asyncio.run(command.callback(interaction, to("off"), None))
    asyncio.run(session._post_transcript("OutHype: hi nova", "Hey!"))
    assert len(posted["logs"]) == 1 and "not posted" in replies[-1]

    asyncio.run(command.callback(interaction, to("voice"), None))
    asyncio.run(session._post_transcript("OutHype: hi nova", "Hey!"))
    assert len(posted["voice"]) == 2
