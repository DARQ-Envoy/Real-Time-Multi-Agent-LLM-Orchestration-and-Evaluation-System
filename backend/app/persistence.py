"""Per-agent log persistence helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def merge_violations(existing: str | None, addition: str | None) -> str | None:
    """Append `addition` to `existing` with a `;` separator. None-safe.

    Used by the budget audit so BUDGET_OVERFLOW does not clobber other
    policy_violations (e.g. SYNTHESIS_LLM_FAIL, FALLBACK_TO_RAG_DRAFT).
    """
    if not addition:
        return existing
    if not existing:
        return addition
    return f"{existing};{addition}"


def sha256_json(obj: Any) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, str):
        text = obj
    else:
        text = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def log_agent_event(
    conn,
    job_id: str | uuid.UUID,
    agent_id: str,
    event_type: str,
    input_hash: str | None = None,
    output_hash: str | None = None,
    latency_ms: float | None = None,
    token_count: int | None = None,
    policy_violations: str | None = None,
) -> None:
    job_uuid = uuid.UUID(job_id) if isinstance(job_id, str) else job_id
    await conn.execute(
        """
        INSERT INTO agent_logs
            (job_id, agent_id, event_type, input_hash, output_hash,
             latency_ms, token_count, policy_violations)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        job_uuid,
        agent_id,
        event_type,
        input_hash,
        output_hash,
        latency_ms,
        token_count,
        policy_violations,
    )
