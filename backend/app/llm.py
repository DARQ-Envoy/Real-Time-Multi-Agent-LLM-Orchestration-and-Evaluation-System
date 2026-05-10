"""Thin Anthropic SDK wrapper for streaming text + forced tool use."""

from __future__ import annotations

from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from .settings import settings

_STUB_PREFIXES = ("stub-", "sk-stub-")


class LLMNotConfigured(Exception):
    pass


class LLMClient:
    def __init__(self) -> None:
        key = (settings.LLM_API_KEY or "").strip()
        if not key or key.startswith(_STUB_PREFIXES):
            self._client: AsyncAnthropic | None = None
        else:
            kwargs: dict[str, Any] = {"api_key": key}
            if settings.LLM_BASE_URL:
                kwargs["base_url"] = settings.LLM_BASE_URL
            self._client = AsyncAnthropic(**kwargs)

    @property
    def configured(self) -> bool:
        return self._client is not None

    def _require(self) -> AsyncAnthropic:
        if self._client is None:
            raise LLMNotConfigured(
                "LLM_API_KEY not set or still using stub default; cannot call Anthropic."
            )
        return self._client

    async def stream_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        client = self._require()
        async with client.messages.stream(
            model=settings.LLM_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            async for delta in stream.text_stream:
                yield delta

    async def call_tool(
        self,
        system: str,
        user: str,
        tool: dict[str, Any],
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Force the model to call `tool` and return its parsed input dict.

        Stream is consumed silently — tool_use input is JSON, not user-facing prose.
        Returns {} if no tool_use block was emitted (caller falls back).
        """
        client = self._require()
        async with client.messages.stream(
            model=settings.LLM_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        ) as stream:
            async for _ in stream:
                pass
            final = await stream.get_final_message()
        for block in final.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
                value = block.input
                return value if isinstance(value, dict) else {}
        return {}
