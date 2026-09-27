"""Picking a voice: searching the list, and remembering the choice."""
from kikomi import voices

LIST = [
    {"id": "en-US-AriaNeural", "gender": "Female", "language": "English (United States)"},
    {"id": "en-GB-RyanNeural", "gender": "Male", "language": "English (United Kingdom)"},
    {"id": "en-GB-SoniaNeural", "gender": "Female", "language": "English (United Kingdom)"},
    {"id": "bg-BG-KalinaNeural", "gender": "Female", "language": "Bulgarian (Bulgaria)"},
]


def ids(query):
    return [v["id"] for v in voices.search(LIST, query)]


def test_search_matches_word_starts():
    assert ids("aria") == ["en-US-AriaNeural"]  # not "Bulgarian"
    assert ids("male british") == ["en-GB-RyanNeural"]  # "male" doesn't match "female"
    assert ids("female uk") == ["en-GB-SoniaNeural"]
    assert ids("en-gb") == ["en-GB-RyanNeural", "en-GB-SoniaNeural"]
    assert len(ids("")) == 4


def _bot(monkeypatch, tmp_path):
    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config
    from kikomi.servers import ServerStore

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def fake_list():
        return LIST

    monkeypatch.setattr(voices, "all_voices", fake_list)
    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    bot.servers = ServerStore(tmp_path / "servers.json", bot.cfg)
    return bot


def test_voice_command_changes_saves_and_resets(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from kikomi.servers import ServerStore

    bot = _bot(monkeypatch, tmp_path)
    guild = SimpleNamespace(id=1, name="test", get_member=lambda uid: None)
    session = bot.session(guild)
    original = session.character.voice["edge"]
    command = next(c for c in bot.tree.get_commands() if c.name == "voice")
    replies = []
    interaction = SimpleNamespace(guild=guild, guild_id=1, response=SimpleNamespace(
        send_message=lambda text, **kw: asyncio.sleep(0, replies.append(text))))

    asyncio.run(command.callback(interaction, name="en-GB-SoniaNeural", speed=10, pitch=None, reset=False))
    assert session.tts.voice == "en-GB-SoniaNeural" and session.tts.rate == "+10%"
    assert "en-GB-SoniaNeural" in replies[-1]

    # Saved: a restart (a fresh store reading the same file) remembers it
    assert ServerStore(tmp_path / "servers.json", bot.cfg).get(1).voices["nova"]["edge"] == "en-GB-SoniaNeural"

    # Only this server changed
    other = bot.session(SimpleNamespace(id=2, name="other", get_member=lambda uid: None))
    assert other.tts.voice == original

    asyncio.run(command.callback(interaction, name="not-a-voice", speed=None, pitch=None, reset=False))
    assert "no voice called" in replies[-1] and session.tts.voice == "en-GB-SoniaNeural"

    asyncio.run(command.callback(interaction, name=None, speed=None, pitch=None, reset=True))
    assert session.tts.voice == original
