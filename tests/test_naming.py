"""Naming the character on a server: the first-join question, the form, and skipping."""
import asyncio
from types import SimpleNamespace

import pytest

from kikomi.config import Character, valid_name


def test_a_renamed_character_answers_to_its_new_name():
    nova = Character(key="nova", name="Nova", persona="You are Nova, a friendly regular.",
                     wake_words=["nova", "novah"], greeting="Hey everyone, Nova here.")
    luna = nova.renamed("Luna Belle")
    assert luna.name == "Luna Belle" and luna.greeting == "Hey everyone, Luna Belle here."
    assert "You are Luna Belle, a friendly regular." in luna.persona and "Your name is Luna Belle." in luna.persona
    assert luna.addressed_in("hey luna, you there?") and not luna.addressed_in("hey nova")
    assert nova.renamed("") is nova and nova.renamed("Nova") is nova


def test_valid_names():
    for good in ["Luna", "Mei-Ling", "O'Neil", "Luna Belle", "諾瓦"]:
        assert valid_name(good), good
    for bad in ["", "A", "R2D2", "@everyone", "a" * 21, "Luna  Belle", "<@123>"]:
        assert not valid_name(bad), bad


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
    return SimpleNamespace(id=gid, name=f"server{gid}", get_member=lambda uid: None, get_channel=lambda cid: None)


def person(manager=True, uid=5):
    return SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(manage_guild=manager), voice=None)


def run_join(bot, g, monkeypatch):
    session = bot.session(g)

    async def fake_join(channel, invited_by=None):
        pass

    monkeypatch.setattr(session, "join", fake_join)
    join = next(c for c in bot.tree.get_commands() if c.name == "join")
    sent = []

    async def send(text, view=None, **kw):
        sent.append((text, view))

    user = person()
    user.voice = SimpleNamespace(channel=object())
    interaction = SimpleNamespace(guild=g, guild_id=g.id, user=user,
                                  response=SimpleNamespace(defer=lambda **kw: asyncio.sleep(0)),
                                  followup=SimpleNamespace(send=send))
    asyncio.run(join.callback(interaction))
    return session, sent


def test_first_join_asks_for_a_name_once(bot, monkeypatch):
    from kikomi.naming import NamePrompt

    g = guild()
    session, sent = run_join(bot, g, monkeypatch)
    prompts = [view for text, view in sent if isinstance(view, NamePrompt)]
    assert len(prompts) == 1 and "What should I be called" in sent[-1][0] and "**Nova**" in sent[-1][0]

    # Choosing a name through the form
    from kikomi.naming import NameForm

    form = NameForm(session)
    form.name_box._value = "Luna"
    replies = []
    interaction = SimpleNamespace(user=person(), response=SimpleNamespace(
        send_message=lambda text, **kw: asyncio.sleep(0, replies.append(text))))
    asyncio.run(form.on_submit(interaction))
    assert session.character.name == "Luna" and "I'm **Luna**" in replies[-1]
    assert bot.servers.get(g.id).name == "Luna"  # saved

    # It doesn't ask again on the next join
    _, sent = run_join(bot, g, monkeypatch)
    assert not any(isinstance(view, NamePrompt) for text, view in sent)


def test_bad_names_are_refused(bot):
    from kikomi.naming import NameForm

    session = bot.session(guild())
    form = NameForm(session)
    form.name_box._value = "@everyone"
    replies = []
    interaction = SimpleNamespace(user=person(), response=SimpleNamespace(
        send_message=lambda text, **kw: asyncio.sleep(0, replies.append(text))))
    asyncio.run(form.on_submit(interaction))
    assert "only have letters" in replies[-1] and session.character.name == "Nova"


def test_skipping_or_ignoring_keeps_nova(bot):
    from kikomi.naming import NamePrompt

    async def keep_pressed():
        session = bot.session(guild(1))
        prompt = NamePrompt(session)
        keep = next(b for b in prompt.children if b.label == "Keep Nova")
        edited = []
        interaction = SimpleNamespace(user=person(), response=SimpleNamespace(
            edit_message=lambda **kw: asyncio.sleep(0, edited.append(kw["content"]))))
        await keep.callback(interaction)
        return session, edited

    session, edited = asyncio.run(keep_pressed())
    assert session.character.name == "Nova" and session.settings.named and "stay **Nova**" in edited[-1]

    async def ignored():
        session = bot.session(guild(2))
        await NamePrompt(session).on_timeout()
        return session

    session = asyncio.run(ignored())
    assert session.character.name == "Nova" and session.settings.named  # won't ask again


def test_only_managers_can_choose_the_name(bot):
    from kikomi.naming import NamePrompt

    async def run():
        prompt = NamePrompt(bot.session(guild()))
        told = []
        someone = SimpleNamespace(user=person(manager=False), response=SimpleNamespace(
            send_message=lambda text, **kw: asyncio.sleep(0, told.append(text))))
        return await prompt.interaction_check(someone), told

    allowed, told = asyncio.run(run())
    assert not allowed and "manage this server" in told[0]


class FakeMe:
    """The bot's own member on a server, recording nickname changes."""

    def __init__(self, nick="Kikomi", forbidden=False):
        self.display_name, self.forbidden, self.edits = nick, forbidden, []

    async def edit(self, nick):
        import discord

        if self.forbidden:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Missing Permissions"), "no")
        self.edits.append(nick)
        self.display_name = nick


def test_nickname_follows_the_chosen_name(bot):
    from kikomi.naming import NameForm

    me = FakeMe()
    g = guild()
    g.me = me
    session = bot.session(g)
    form = NameForm(session)
    form.name_box._value = "Luna"
    replies = []
    interaction = SimpleNamespace(user=person(), response=SimpleNamespace(
        send_message=lambda text, **kw: asyncio.sleep(0, replies.append(text))))
    asyncio.run(form.on_submit(interaction))
    assert me.edits == ["Luna"] and "Change Nickname" not in replies[-1]

    asyncio.run(session.sync_nickname())  # already right: no extra change
    assert me.edits == ["Luna"]


def test_keeping_nova_also_sets_the_nickname(bot):
    from kikomi.naming import NamePrompt

    me = FakeMe()
    g = guild()
    g.me = me

    async def run():
        prompt = NamePrompt(bot.session(g))
        keep = next(b for b in prompt.children if b.label == "Keep Nova")
        await keep.callback(SimpleNamespace(user=person(), response=SimpleNamespace(
            edit_message=lambda **kw: asyncio.sleep(0))))

    asyncio.run(run())
    assert me.edits == ["Nova"]


def test_missing_permission_is_explained(bot):
    g = guild()
    g.me = FakeMe(forbidden=True)
    session = bot.session(g)
    session.update(name="Luna", named=True)
    note = asyncio.run(session.sync_nickname())
    assert "Change Nickname" in note
