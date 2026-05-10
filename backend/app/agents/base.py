from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from ..models import SSEEvent, SharedContext


class AgentBase(ABC):
    agent_id: str = "base"
    max_context_tokens: int = 4096

    @abstractmethod
    def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        """Yield SSEEvents while executing. Implementations are async generators."""
        raise NotImplementedError
