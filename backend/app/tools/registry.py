"""Tool registry — name -> ToolFn mapping. Orchestrator emits names from this map."""

from __future__ import annotations

from . import code_exec, self_reflection, sql_lookup, web_search
from .runner import ToolFn

REGISTRY: dict[str, ToolFn] = {
    "web_search": web_search.run,
    "code_exec": code_exec.run,
    "sql_lookup": sql_lookup.run,
    "self_reflection": self_reflection.run,
}


def lookup(name: str) -> ToolFn:
    return REGISTRY[name]


__all__ = ["REGISTRY", "lookup"]
