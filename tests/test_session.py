"""Reply pipeline with fake model, voice and speaker - no Discord connection."""
import asyncio
from types import SimpleNamespace

from kikomi.config import DEFAULTS, Character
from kikomi.session import GuildSession
from kikomi.tts import DEFAULT_MOODS, Clip


class FakeLLM:
    def __init__(self, chunks, delay=0.0):
        self.chunks, self.delay, self.calls = chunks, delay, []

    async def stream(self, system, messages):
        self.calls.append((system, [dict(m) for m in messages]))
        for c in self.chunks:
            await asyncio.sleep(self.delay)
            yield c


class FakeTTS:
    async def synth(self, text, mood="neutral"):
        await asyncio.sleep(0.05 if "first" in text else 0.0)  # first clip is slowest
        return Clip(text.encode(), mood)  # the mood rides along in place of real filters


class FakeSpeaker:
    def __init__(self):
        self.played, self.spoken, self.stopped = [], [], False

    busy = property(lambda self: bool(self.played) and not self.stopped)

    def say(self, clip, text):
        self.played.append(clip.audio.decode())
        self.moods = getattr(self, "moods", []) + [clip.filters]
        self.spoken.append(text)

    def stop(self):
        self.stopped = True

    async def wait_idle(self):
        pass


def make_session(llm):
    from kikomi.servers import ServerStore

    cfg = {**DEFAULTS, "privacy": {"require_opt_in": True, "post_transcripts": False}}
    bot = SimpleNamespace(cfg=cfg, llm=llm, servers=ServerStore(None, cfg))
    s = GuildSession(bot, SimpleNamespace(id=1, name="test", get_member=lambda uid: None))
    s.tts = FakeTTS()
    s.character = Character(key="nova", name="Nova", persona="You are Nova.", wake_words=["nova"],
                            moods=DEFAULT_MOODS)
    s.speaker = FakeSpeaker()
    return s


def test_reply_is_spoken_in_order_and_remembered():
    llm = FakeLLM(["This is the first sentence. ", "Then a *grin* second one! ", "And **done**"])
    s = make_session(llm)

    async def run():
        s.conversation.hear("Ana", "hey nova")
        s._start_reply()
        await s._reply_task

    asyncio.run(run())
    assert s.speaker.played == ["This is the first sentence.", "Then a second one!", "And done"]
    assert s.conversation.history[-1] == {"role": "assistant",
                                          "content": "This is the first sentence. Then a *grin* second one! And **done**"}
    system, messages = llm.calls[0]
    assert "Discord voice channel" in system and messages == [{"role": "user", "content": "Ana: hey nova"}]


def test_wake_word_and_followup():
    s = make_session(FakeLLM([]))
    assert s._should_answer(5, "hey Nova", 0.0) and not s._should_answer(5, "hello there", 0.0)
    s._last_reply_to, s._last_reply_at = {5}, 1000.0  # bot finished answering user 5
    assert s._should_answer(5, "and what else", 1005.0)
    assert not s._should_answer(6, "and what else", 1005.0)  # someone else still needs the name
    assert not s._should_answer(5, "and what else", 1000.0 + 31)  # too late


def test_long_followup_that_started_in_time_counts():
    # The bug from the first live test: the answer started 17 s after the bot
    # stopped talking but ran for 5.5 s, so it finished after the window.
    s = make_session(FakeLLM([]))
    s.listen_cfg = {**s.listen_cfg, "followup_seconds": 20}
    s._last_reply_to, s._last_reply_at = {5}, 1000.0
    assert s._should_answer(5, "Oh, I didn't complete the pantheon", 1017.0)


def test_interrupt_cancels_and_keeps_what_was_said():
    llm = FakeLLM(["Okay so here is a long answer. ", "It keeps going on and on. ", "Forever and ever. "], delay=0.05)
    s = make_session(llm)

    async def run():
        s.conversation.hear("Ana", "nova tell me something")
        s._last_reply_to = {7}
        s._start_reply()
        await asyncio.sleep(0.08)  # first sentence out
        s._on_speech_start(7)
        try:
            await s._reply_task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert s.speaker.stopped
    assert s.conversation.history[-1]["content"].endswith("[cut off]")
    assert "Forever" not in s.conversation.history[-1]["content"]


def test_bot_builds_and_registers_commands(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config

    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    names = sorted(c.name for c in bot.tree.get_commands())
    assert names == ["character", "help", "join", "leave", "listen", "mode", "optin", "optout",
                     "rejoin", "reset", "say", "setup", "status", "stop", "transcripts", "voice"]


def test_help_lists_every_command(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from kikomi.bot import HELP_TEXT, KikomiBot
    from kikomi.config import ROOT, load_config

    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    missing = [c.name for c in bot.tree.get_commands() if c.name != "help" and f"/{c.name}" not in HELP_TEXT]
    assert missing == []  # add new commands to HELP_TEXT in kikomi/bot.py


class FakeInteraction:
    """Just enough of a Discord interaction to run a command."""

    def __init__(self, guild, user_id=5):
        self.guild, self.guild_id = guild, guild.id
        self.user = SimpleNamespace(id=user_id, voice=None)
        self.replies = []

        async def send_message(text, **kw):
            self.replies.append(text)

        self.response = SimpleNamespace(send_message=send_message)


def test_listen_stop_say_and_status_commands(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config

    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    guild = SimpleNamespace(id=1, name="test", get_member=lambda uid: None)
    cmd = {c.name: c for c in bot.tree.get_commands()}
    i = FakeInteraction(guild)
    session = bot.session(guild)
    monkeypatch.setattr(bot.consent, "allowed", lambda g, u: True)

    # /listen off: nobody is heard, even people who opted in
    asyncio.run(cmd["listen"].callback(i, SimpleNamespace(value="off")))
    assert not session._allowed(5) and "stopped listening" in i.replies[-1]
    asyncio.run(cmd["listen"].callback(i, SimpleNamespace(value="on")))
    assert session._allowed(5)

    # not in a voice channel: /stop and /say explain instead of failing
    asyncio.run(cmd["stop"].callback(i))
    assert "not in a voice channel" in i.replies[-1]

    # in a voice channel: /stop cuts the bot off, /say speaks in the chosen mood
    stopped, said = [], []
    session.vc = SimpleNamespace(is_connected=lambda: True, channel=SimpleNamespace(mention="#kikomi-test"))
    monkeypatch.setattr(session, "stop_talking", lambda: stopped.append(True))

    async def fake_speak(text, mood="happy"):
        said.append((text, mood))

    monkeypatch.setattr(session, "speak", fake_speak)
    asyncio.run(cmd["stop"].callback(i))
    assert stopped == [True]
    asyncio.run(cmd["say"].callback(i, "Hello everyone!", "happy"))
    assert said == [("Hello everyone!", "happy")]
    asyncio.run(cmd["say"].callback(i, "Hi", "grumpy"))
    assert "don't have a `grumpy` mood" in i.replies[-1] and len(said) == 1

    # /status tells you whether you can be heard
    asyncio.run(cmd["status"].callback(i))
    assert "#kikomi-test" in i.replies[-1] and "You've opted in" in i.replies[-1]


def test_moods_follow_the_tags_and_stay_out_of_the_transcript():
    llm = FakeLLM(["[happy] You actually beat it? ", "That's amazing! ", "[sad] I never got past the first boss... ", "[wink] ok"])
    s = make_session(llm)

    async def run():
        s.conversation.hear("Ana", "nova I beat it")
        s._start_reply()
        await s._reply_task

    asyncio.run(run())
    assert s.speaker.played == ["You actually beat it?", "That's amazing!", "I never got past the first boss...", "[wink] ok"]
    assert s.speaker.moods == ["happy", "happy", "sad", "sad"]  # a mood lasts until the next tag; unknown tags stay text
    assert s.conversation.history[-1]["content"].startswith("[happy] You actually")  # remembered with tags
    system, _ = llm.calls[0]
    assert "[happy]" in system and "[shy]" in system  # the AI is told which moods it can use


def test_unaddressed_lines_are_marked_overheard():
    s = make_session(FakeLLM(["[neutral] Sure."]))

    async def run():
        from kikomi.listener import Utterance
        s.bot.transcriber = SimpleNamespace(transcribe=lambda audio, hotwords=None, languages=None: next(lines))
        s.bot.stt_executor = None
        silence = bytes(3840)
        await s._on_utterance(Utterance(5, silence, 0.0))  # no wake word: overheard
        await s._on_utterance(Utterance(5, silence, 0.0))  # says the name: answered
        await s._reply_task

    lines = iter(["I beat the pantheon", "hey nova, what do you think?"])
    asyncio.run(run())
    first_turn = [m for m in s.bot.llm.calls[0][1] if m["role"] == "user"][-1]["content"]
    assert first_turn.splitlines() == ["(overheard) User 5: I beat the pantheon", "User 5: hey nova, what do you think?"]


def test_interrupting_keeps_the_bot_listening():
    # The bug from the second live test: stopping the voice used vc.stop(),
    # which on the listening client also stops hearing anyone.
    from kikomi.speaker import Speaker

    calls = []
    vc = SimpleNamespace(is_playing=lambda: True, stop=lambda: calls.append("stop"),
                         stop_playing=lambda: calls.append("stop_playing"))

    async def run():
        speaker = Speaker(lambda: vc)
        speaker.stop()
        speaker.close()

    asyncio.run(run())
    assert calls == ["stop_playing", "stop_playing"]  # never the stop() that also stops listening


def test_restart_leaves_leftover_voice_channels(monkeypatch):
    # After a crash or forced stop, Discord still shows the bot in the channel;
    # on start-up it should leave. A channel it joined this time is left alone.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config

    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    left = []

    def guild(gid, in_voice):
        async def change_voice_state(channel):
            left.append(gid)

        me = SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(name="vc")) if in_voice else None)
        return SimpleNamespace(id=gid, name=f"g{gid}", me=me, change_voice_state=change_voice_state)

    guilds = [guild(1, True), guild(2, False), guild(3, True)]
    monkeypatch.setattr(KikomiBot, "guilds", property(lambda self: guilds))
    bot.sessions[3] = SimpleNamespace(connected=True)  # joined after starting up: keep it

    asyncio.run(bot.leave_leftover_channels())
    assert left == [1]


def test_shutdown_leaves_voice_first(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import discord

    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config

    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    order = []

    async def leave():
        order.append("left voice")

    async def parent_close(self):
        order.append("closed")

    bot.sessions[1] = SimpleNamespace(connected=True, leave=leave)
    bot.sessions[2] = SimpleNamespace(connected=False, leave=leave)
    monkeypatch.setattr(discord.Client, "close", parent_close)
    asyncio.run(bot.close())
    assert order == ["left voice", "closed"]


def test_greeting_can_be_answered_without_the_name():
    # From live tests: after /join and the greeting, "How are you doing today?"
    # was ignored for lacking the name.
    s = make_session(FakeLLM([]))
    s.vc = None

    class FakeChannel:
        name = "vc"
        async def connect(self, **kw):
            return SimpleNamespace(listen=lambda sink: None, is_connected=lambda: True,
                                   channel=SimpleNamespace(id=9, name="vc"))

    async def fake_speak(text, mood="happy"):
        pass

    s.speak = fake_speak
    s.character.greeting = "Hey everyone!"
    asyncio.run(s.join(FakeChannel(), invited_by=5))
    now = __import__("time").monotonic()
    assert s._should_answer(5, "How are you doing today?", now)  # the person who ran /join
    assert not s._should_answer(6, "How are you doing today?", now)  # others still use the name


def test_leave_explains_a_leftover_from_a_sudden_stop(monkeypatch):
    # From live tests: after a forced stop the bot kept showing in the channel for
    # ~30s whatever the new run did. /leave still asks Discord, and says honestly
    # that Discord clears it on its own.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config

    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    left, replies = [], []

    async def change_voice_state(channel):
        left.append(channel)

    g = SimpleNamespace(id=1, name="test", change_voice_state=change_voice_state,
                        me=SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(name="vc"))))
    interaction = SimpleNamespace(guild=g, guild_id=1, response=SimpleNamespace(
        send_message=lambda text, **kw: asyncio.sleep(0, replies.append(text))))
    leave = next(c for c in bot.tree.get_commands() if c.name == "leave")

    asyncio.run(leave.callback(interaction))
    assert left == [None] and "within about 30 seconds" in replies[-1]


def test_stop_request_shuts_down_properly(monkeypatch, tmp_path):
    # `python -m kikomi --stop` creates the stop file; the bot then leaves voice
    # and closes, instead of being killed and lingering in the channel.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import kikomi.bot as botmod
    from kikomi.bot import KikomiBot
    from kikomi.config import ROOT, load_config

    stop_file = tmp_path / "stop"
    monkeypatch.setattr(botmod, "STOP_FILE", stop_file)
    bot = KikomiBot(load_config(ROOT / "config.example.yaml"))
    closed = []

    async def fake_close():
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)
    monkeypatch.setattr(bot, "is_closed", lambda: bool(closed))
    stop_file.touch()
    asyncio.run(asyncio.wait_for(bot._watch_for_stop(), 5))
    assert closed == [True] and not stop_file.exists()
