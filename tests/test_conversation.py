from kikomi.config import Character, load_config, load_character, ROOT
from kikomi.conversation import Conversation


def test_turns_group_speakers_and_trim():
    c = Conversation(max_turns=3)
    c.hear("Ana", "hi nova")
    c.hear("Ben", "hey")
    assert c.take_turn() == [{"role": "user", "content": "Ana: hi nova\nBen: hey"}]
    c.reply("Hi both!")
    c.hear("Ana", "how are you")
    msgs = c.take_turn()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    c.reply("Great")
    c.hear("Ben", "cool")
    msgs = c.take_turn()
    assert msgs[0]["role"] == "user" and len(msgs) <= 3


def test_wake_words():
    ch = Character(key="n", name="Nova", persona="", wake_words=["nova", "novah"])
    assert ch.addressed_in("hey Nova what's up")
    assert not ch.addressed_in("supernova is a word")
    # From a live test: "Hi Nova" was transcribed as one word.
    assert ch.addressed_in("Inova.") and ch.addressed_in("Heynova, you there?")
    assert not ch.addressed_in("Casanova was here") and not ch.addressed_in("innovation")


def test_example_config_and_character_load():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg["llm"]["model"] == "claude-opus-5" and cfg["listening"]["mode"] == "wake"
    assert load_character("nova").name == "Nova"


def test_wake_word_forgives_near_misses():
    # From live tests: "Hey Nova" came out as "Hey, Noga." and "Inova."
    ch = Character(key="n", name="Nova", persona="", wake_words=["nova"])
    for heard in ["Hey, Noga.", "Inova.", "hey nora can you hear me", "Nava?", "Heynova"]:
        assert ch.addressed_in(heard), heard
    for heard in ["supernova", "I read a novel", "not now", "Noah said hi", "nope", "over there"]:
        assert not ch.addressed_in(heard), heard
