"""The AI "brain" that writes the character's replies.

There are two options, picked with ``llm.provider`` in config.yaml:

* ``anthropic``: Claude, through Anthropic's official library. Needs an API key.
* ``openai_compatible``: any AI server that speaks the same language as
  OpenAI's API. That includes free programs that run models on your own
  computer, such as Ollama, LM Studio, llama.cpp and vLLM.

Either way, the reply comes back a few words at a time ("streaming"), so the
bot can start speaking before the whole reply is written.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator, Dict, List, Optional, Protocol

log = logging.getLogger(__name__)

# One message in the conversation, e.g. {"role": "user", "content": "Ana: hi!"}
Message = Dict[str, str]


class LLM(Protocol):
    """What every AI option must be able to do: take the character description
    (``system``) and the conversation so far, and stream back a reply."""

    def stream(self, system: str, messages: List[Message]) -> AsyncIterator[str]: ...


class AnthropicLLM:
    """Replies written by Claude."""

    def __init__(
        self,
        model: str = "claude-opus-5",
        max_tokens: int = 4096,
        effort: Optional[str] = "low",
        fallbacks: bool = True,
    ) -> None:
        import anthropic

        # The API key is read automatically from ANTHROPIC_API_KEY in .env
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.max_tokens = max_tokens  # upper limit on reply length
        self.effort = effort  # how hard Claude thinks first; "low" gives the quickest replies
        self.fallbacks = fallbacks

    async def stream(self, system: str, messages: List[Message]) -> AsyncIterator[str]:
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            # The conversation only grows at the end, so Anthropic can remember
            # the earlier part between requests. That makes replies cheaper and faster.
            cache_control={"type": "ephemeral"},
        )
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            # If Claude declines to answer, Anthropic automatically tries again
            # with another suitable model, all within this one request.
            manager = self.client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            manager = self.client.messages.stream(**kwargs)
        async with manager as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        # Explain in the log if a reply was declined or cut short.
        if final.stop_reason == "refusal":
            log.warning("Model declined to answer (%s)", getattr(final.stop_details, "category", None))
        elif final.stop_reason == "max_tokens":
            log.warning("Reply hit max_tokens (%s); raise llm.max_tokens if replies get cut off", self.max_tokens)


class OpenAICompatibleLLM:
    """Replies from an OpenAI-style server, such as a model running on your own PC."""

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434/v1",
        api_key_env: str = "OPENAI_API_KEY",
        max_tokens: int = 1024,
    ) -> None:
        from openai import AsyncOpenAI

        # Local servers don't check the key, but the library refuses to run
        # without one, so we pass a placeholder when none is set.
        self.client = AsyncOpenAI(base_url=base_url, api_key=os.environ.get(api_key_env) or "not-needed")
        self.model = model
        self.max_tokens = max_tokens

    async def stream(self, system: str, messages: List[Message]) -> AsyncIterator[str]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            max_tokens=self.max_tokens,
            stream=True,
        )
        thinking = ThinkFilter()
        async for chunk in response:
            if not chunk.choices:
                continue
            text = thinking.feed(chunk.choices[0].delta.content or "")
            if text:
                yield text


class ThinkFilter:
    """Hides a model's "thinking out loud" so the bot doesn't speak it.

    Some local models (like Qwen3 and DeepSeek-R1) write their reasoning
    between <think> and </think> before the actual answer. The reply arrives
    in small pieces, and a tag can be split across two pieces ("<thi" + "nk>"),
    so we keep a little text back whenever a piece ends in what might be the
    start of a tag.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.inside = False  # are we currently inside a <think> section?
        self.buf = ""  # text waiting to be checked

    def feed(self, text: str) -> str:
        """Add the next piece of the reply; returns the part that's safe to show."""
        self.buf += text
        out = []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                if not self.inside:
                    out.append(self.buf[:i])  # the text before <think> is part of the answer
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            # No full tag found. If the text ends with the beginning of one
            # (say "</th"), hold that bit back until the next piece arrives.
            keep = next((k for k in range(len(tag) - 1, 0, -1) if self.buf.endswith(tag[:k])), 0)
            if not self.inside:
                out.append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:] if keep else ""
            break
        return "".join(out)


def make_llm(cfg: dict) -> LLM:
    """Build the AI option chosen in the ``llm`` section of config.yaml."""
    provider = cfg.get("provider", "anthropic")
    if provider == "anthropic":
        return AnthropicLLM(
            model=cfg.get("model", "claude-opus-5"),
            max_tokens=cfg.get("max_tokens", 4096),
            effort=cfg.get("effort", "low"),
            fallbacks=cfg.get("fallbacks", True),
        )
    if provider == "openai_compatible":
        return OpenAICompatibleLLM(
            model=cfg["model"],
            base_url=cfg.get("base_url", "http://localhost:11434/v1"),
            api_key_env=cfg.get("api_key_env", "OPENAI_API_KEY"),
            max_tokens=cfg.get("max_tokens", 1024),
        )
    raise ValueError(f"Unknown llm.provider '{provider}' (use 'anthropic' or 'openai_compatible')")
