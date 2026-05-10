from __future__ import annotations

import asyncio
import hashlib
from typing import AsyncIterator

from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    SharedContext,
    SSEEvent,
    TokenEvent,
)
from .base import AgentBase

STUB_TOKENS = ("Routing query.", "Looking up.", "Done.")
TOKEN_DELAY_SECONDS = 0.2


class StubAgent(AgentBase):
    agent_id = "stub"
    max_context_tokens = 4096

    async def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=self.max_context_tokens)
        for token in STUB_TOKENS:
            await asyncio.sleep(TOKEN_DELAY_SECONDS)
            yield TokenEvent(agent_id=self.agent_id, text=token)
        joined = "".join(STUB_TOKENS)
        output_hash = hashlib.sha256(joined.encode("utf-8")).hexdigest()
        yield AgentEndEvent(
            agent_id=self.agent_id,
            output_hash=output_hash,
            policy_violations=None,
        )
