from kikomi.llm import ThinkFilter
from kikomi.speech_text import SentenceSplitter, clean_for_speech


def feed_all(chunks):
    s = SentenceSplitter()
    out = []
    for c in chunks:
        out += s.feed(c)
    return out + s.flush()


def test_splits_streamed_sentences():
    chunks = ["Hey there, Alex! How", "'s it going? I was just", " thinking about games.", " Want to play?"]
    assert feed_all(chunks) == ["Hey there, Alex!", "How's it going?", "I was just thinking about games.", "Want to play?"]


def test_waits_for_decimal_numbers():
    s = SentenceSplitter()
    assert s.feed("The version is 3.") == []
    assert s.feed("5 now and it works. ") == ["The version is 3.5 now and it works."]


def test_short_fragments_are_glued_on():
    assert feed_all(["Oh. ", "That is really cool. "]) == ["Oh. That is really cool."]


def test_clean_for_speech():
    assert clean_for_speech("*waves* Hi **there** 😄 see https://x.com") == "Hi there see a link"


def test_think_filter_across_chunks():
    f = ThinkFilter()
    out = "".join(f.feed(c) for c in ["<thi", "nk>plan stuff</th", "ink>Hello", " <think>x</think>world"])
    assert out == "Hello world"


def test_take_mood_and_strip():
    from kikomi.speech_text import strip_moods, take_mood
    from kikomi.tts import DEFAULT_MOODS

    assert take_mood("[happy] Hi there!", DEFAULT_MOODS, "neutral") == ("Hi there!", "happy")
    assert take_mood("No tag here.", DEFAULT_MOODS, "sad") == ("No tag here.", "sad")
    assert take_mood("[HAPPY]Yay [note] this", DEFAULT_MOODS, "neutral") == ("Yay [note] this", "happy")
    assert strip_moods("[happy] You won! [surprised] Wait, what?", DEFAULT_MOODS) == "You won! Wait, what?"


def test_tone_filters():
    from kikomi.tts import tone_filters

    assert tone_filters({}) == ""
    f = tone_filters({"speed": 1.1, "pitch": 1.0, "warmth": 0.5, "gain_db": -3})
    assert "asetrate=50854" in f and "atempo=1.0383" in f and "bass=g=1.50" in f and "volume=-3.0dB" in f


def test_backup_voice_speaks_what_the_main_voice_cannot():
    # From a live test: an English voice produced no sound for a Chinese reply.
    import asyncio

    from edge_tts.exceptions import NoAudioReceived

    from kikomi.tts import EdgeTTS

    tts = EdgeTTS("en-US-AriaNeural", backup="en-US-EmmaMultilingualNeural")
    used = []

    async def fake_speak(text, voice):
        used.append(voice)
        if voice == "en-US-AriaNeural" and not text.isascii():
            raise NoAudioReceived("no audio")
        return b"mp3"

    tts._speak = fake_speak
    assert asyncio.run(tts.synth("Hello!")).audio == b"mp3" and used == ["en-US-AriaNeural"]
    used.clear()
    assert asyncio.run(tts.synth("你好!")).audio == b"mp3"
    assert used == ["en-US-AriaNeural", "en-US-EmmaMultilingualNeural"]


def test_splits_chinese_sentences():
    # Chinese punctuation has no space after it, so it needs its own rule.
    s = SentenceSplitter(min_chars=4)
    out = s.feed("會啊，我中文還行！你想聊")
    out += s.feed("什麼？我可能會講得怪怪的。")
    out += s.flush()
    assert out == ["會啊，我中文還行！", "你想聊什麼？",
                   "我可能會講得怪怪的。"]
