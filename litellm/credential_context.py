"""Shared by normal imports and LiteLLM's file-based custom-auth loader."""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(repr=False)
class IncomingCredential:
    value: str | None

    def __repr__(self) -> str:
        return "IncomingCredential(<redacted>)"


incoming_credential: ContextVar[IncomingCredential | None] = ContextVar(
    "litellm_incoming_credential", default=None,
)
