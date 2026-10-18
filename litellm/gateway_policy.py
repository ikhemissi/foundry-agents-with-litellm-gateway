"""Pure route and request-body policy shared by the transport boundary and the auth hook.

Nothing here holds state or credentials, so it is safe for the module to be imported twice
(once normally, once by LiteLLM's file-based custom_auth loader).
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

MODEL = "gpt-5.4-nano"
NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
HEALTH_ROUTES = frozenset({"/health/liveliness", "/health/readiness"})
INFERENCE_ROUTES = frozenset({
    "/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses",
})
PROVIDER_OVERRIDES = frozenset({
    "api_key", "api_base", "base_url", "api_version", "custom_llm_provider",
    "headers", "extra_headers", "proxy_server_request", "litellm_metadata",
})
INFERENCE_FIELDS = frozenset({
    "model", "messages", "input", "instructions", "stream", "stream_options",
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens",
    "frequency_penalty", "presence_penalty", "stop", "seed", "n", "logprobs",
    "top_logprobs", "logit_bias", "response_format", "text", "reasoning",
    "reasoning_effort", "verbosity", "tools", "tool_choice", "parallel_tool_calls",
    "prediction", "modalities", "audio", "service_tier", "store", "include",
    "truncation", "previous_response_id", "prompt_cache_key", "prompt_cache_retention",
    "safety_identifier", "user", "metadata",
})

# Only the JSON-RPC methods a tutor needs; prompts, resources, sampling, elicitation and
# subscriptions stay closed.
MCP_METHODS = frozenset({"initialize", "notifications/initialized", "tools/list", "tools/call", "ping"})
# A2A 1.0 only: PascalCase JSON-RPC, no legacy 0.3 `message/send`, no streaming or task methods.
A2A_METHODS = frozenset({"SendMessage"})
JSONRPC_FIELDS = frozenset({"jsonrpc", "id", "method", "params"})
A2A_PARAMS = frozenset({"message", "configuration", "metadata"})
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

MAX_BODY_BYTES = {"inference": 4_194_304, "mcp": 262_144, "a2a": 65_536}
BASE_HEADERS = frozenset({b"content-type", b"accept", b"host"})
EXTRA_HEADERS = {
    "mcp": frozenset({b"mcp-protocol-version", b"mcp-session-id"}),
    "a2a": frozenset({b"a2a-version"}),
}
MAX_EXTRA_HEADER_LENGTH = 200

_MCP_PATH = re.compile(r"^/mcp(?:/([a-z][a-z0-9_]{0,40}))?$")
_A2A_PATH = re.compile(r"^/a2a/([a-z][a-z0-9_]{0,40})$")
_A2A_CARD_PATH = re.compile(r"^/a2a/([a-z][a-z0-9_]{0,40})/\.well-known/agent-card\.json$")


@dataclass(frozen=True)
class Route:
    kind: str  # health | inference | mcp | a2a | a2a_card
    resource: str | None = None

    @property
    def has_body(self) -> bool:
        return self.kind in ("inference", "mcp", "a2a")

    @property
    def body_class(self) -> str:
        return "a2a" if self.kind == "a2a" else self.kind


def classify(method: str, path: str) -> Route | None:
    """Return the permitted route class, or None. Exact matches only."""
    if method == "GET":
        if path in HEALTH_ROUTES:
            return Route("health")
        card = _A2A_CARD_PATH.match(path)
        return Route("a2a_card", card.group(1)) if card else None
    if method != "POST":
        return None
    if path in INFERENCE_ROUTES:
        return Route("inference")
    mcp = _MCP_PATH.match(path)
    if mcp:
        return Route("mcp", mcp.group(1))
    agent = _A2A_PATH.match(path)
    if agent:
        return Route("a2a", agent.group(1))
    return None


def validate_body(route: Route, body: dict[str, Any]) -> str | None:
    """Return a rejection message, or None when the body is acceptable for the route."""
    if route.kind == "inference":
        if PROVIDER_OVERRIDES.intersection(body):
            return "Provider overrides are not permitted."
        if body.keys() - INFERENCE_FIELDS:
            return "Unsupported inference options."
        return None
    allowed_methods = MCP_METHODS if route.kind == "mcp" else A2A_METHODS
    if body.keys() - JSONRPC_FIELDS or body.get("jsonrpc") != "2.0":
        return "Unsupported JSON-RPC request."
    if body.get("method") not in allowed_methods:
        return "JSON-RPC method is not permitted."
    request_id = body.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        return "Unsupported JSON-RPC request."
    params = body.get("params", {})
    if not isinstance(params, dict):
        return "Unsupported JSON-RPC request."
    if route.kind == "a2a":
        if params.keys() - A2A_PARAMS or not isinstance(params.get("message"), dict):
            return "Unsupported A2A request."
    elif body["method"] == "tools/call":
        if not isinstance(params.get("name"), str) or not TOOL_NAME.match(params["name"]):
            return "Unsupported MCP tool call."
        if not isinstance(params.get("arguments", {}), dict):
            return "Unsupported MCP tool call."
    return None


def forwardable_headers(route: Route, headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Strict allowlist: alternate API keys, baggage and identity headers never reach LiteLLM."""
    extra = EXTRA_HEADERS.get(route.body_class, frozenset())
    kept = []
    for name, value in headers:
        lowered = name.lower()
        if lowered in BASE_HEADERS:
            kept.append((lowered, value))
        elif lowered in extra and len(value) <= MAX_EXTRA_HEADER_LENGTH and value.isascii() and value.decode().isprintable():
            kept.append((lowered, value))
    return kept


def a2a_version_ok(headers: list[tuple[bytes, bytes]]) -> bool:
    """A2A calls must declare protocol 1.x exactly once; legacy callers are refused."""
    values = [value for name, value in headers if name.lower() == b"a2a-version"]
    return len(values) == 1 and values[0].startswith(b"1.") and len(values[0]) <= 16


_SAFE = re.compile(r"[^A-Za-z0-9_./:@-]")


def safe(value: object, limit: int = 64) -> str:
    """Make an untrusted value safe for one-line audit logging (never used for secrets)."""
    text = _SAFE.sub("?", str(value))[:limit]
    return text or "-"


AUDIT = logging.getLogger("gateway.audit")


def audit(decision: str, reason: str, **fields: object) -> None:
    """One nonsecret line per decision: identifiers, route and method only, never tokens or bodies."""
    pairs = " ".join(f"{key}={safe(value)}" for key, value in fields.items() if value is not None)
    AUDIT.info("decision=%s reason=%s %s", safe(decision), safe(reason), pairs)


def rpc_method(body: object) -> str | None:
    method = body.get("method") if isinstance(body, dict) else None
    return safe(method) if isinstance(method, str) else None
