"""Optional language-model access through the LiteLLM gateway.

The model only ever polishes wording; it can never influence answers, scores or pass/fail. Every failure
(timeout, non-200, bad output, missing credentials) degrades to `None` and is logged by error class only:
prompts, answers, tokens and response bodies are never logged.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from typing import Protocol
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

log = logging.getLogger("quiz.model")

DEFAULT_MODEL = "gpt-5.4-nano"
CALL_TIMEOUT_SECONDS = 8.0
TOTAL_TIMEOUT_SECONDS = 12.0
COOLDOWN_SECONDS = 20.0
MAX_CONCURRENT_CALLS = 4


class QuizModel(Protocol):
    async def complete_json(self, system: str, user: dict, *, max_tokens: int) -> dict | None:
        """Return the model's JSON object, or None on any failure."""


class ModelConfigError(ValueError):
    pass


def _origin(url: str | httpx.URL) -> tuple[str, str, int]:
    parsed = httpx.URL(str(url))
    return parsed.scheme, parsed.host, parsed.port or (443 if parsed.scheme == "https" else 80)


class RequestCredentialAuth(httpx.Auth):
    """Attach a bearer token to each request of one origin only; the client never stores it."""

    def __init__(self, get_token: Callable[[], Awaitable[str]], origin: str) -> None:
        self.get_token = get_token
        self.origin = _origin(origin)

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if _origin(request.url) != self.origin:
            raise RuntimeError("The gateway credential is bound to the gateway origin.")
        request.headers["Authorization"] = f"Bearer {await self.get_token()}"
        try:
            yield request
        finally:
            request.headers.pop("Authorization", None)


class GatewayModel:
    """Chat completions against the gateway's OpenAI-compatible endpoint with a per-request bearer token."""

    def __init__(
        self,
        base_url: str,
        model: str,
        token_provider: Callable[[], Awaitable[str]],
        *,
        call_timeout: float = CALL_TIMEOUT_SECONDS,
        total_timeout: float = TOTAL_TIMEOUT_SECONDS,
        cooldown: float = COOLDOWN_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        validate_base_url(base_url)
        # The SDK loggers print request options (prompts) at DEBUG; keep content out of any log level.
        for noisy in ("openai", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        self.model = model
        self.total_timeout = total_timeout
        self.cooldown = cooldown
        self._retry_at = 0.0
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        self._http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(call_timeout, connect=min(3.0, call_timeout)),
            auth=RequestCredentialAuth(token_provider, f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"),
        )
        self._client = AsyncOpenAI(base_url=base_url, api_key="request-local-credential", max_retries=0, http_client=self._http)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete_json(self, system: str, user: dict, *, max_tokens: int) -> dict | None:
        if time.monotonic() < self._retry_at:
            return None
        try:
            async with self._slots:
                content = await asyncio.wait_for(self._complete(system, user, max_tokens), self.total_timeout)
        except Exception as error:  # noqa: BLE001 - degrade to deterministic behaviour; never log bodies or tokens
            self._retry_at = time.monotonic() + self.cooldown
            log.warning("model call failed: %s", type(error).__name__)
            return None
        try:
            result = json.loads(content)
        except (TypeError, ValueError):
            log.warning("model returned unusable output")
            return None
        return result if isinstance(result, dict) else None

    async def _complete(self, system: str, user: dict, max_tokens: int) -> str:
        # No temperature or user/metadata: gpt-5 family models reject non-default sampling and the gateway
        # strips caller identity fields anyway.
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            raise ValueError("incomplete model response")
        return choice.message.content


def validate_base_url(base_url: str) -> str:
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.path.rstrip("/") != "/v1"
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or "?" in base_url
        or "#" in base_url
    ):
        raise ModelConfigError("QUIZ_GATEWAY_BASE_URL must be an HTTPS gateway /v1 endpoint without credentials or query.")
    return base_url


def _guid(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    try:
        if str(uuid.UUID(value)) != value.lower():
            raise ValueError
    except ValueError:
        raise ModelConfigError(f"{name} must be a GUID.") from None
    return value.lower()


def model_from_env(env: Mapping[str, str] | None = None) -> GatewayModel | None:
    """None when QUIZ_MODEL_MODE is off/unset; a GatewayModel in `gateway` mode; fails fast on bad config."""
    env = os.environ if env is None else env
    mode = env.get("QUIZ_MODEL_MODE", "off").strip().lower() or "off"
    if mode == "off":
        return None
    if mode != "gateway":
        raise ModelConfigError("QUIZ_MODEL_MODE must be 'off' or 'gateway'.")
    base_url = validate_base_url(env.get("QUIZ_GATEWAY_BASE_URL", "").strip())
    scope = f"api://{_guid(env, 'QUIZ_GATEWAY_AUDIENCE')}/.default"
    client_id = _guid(env, "AZURE_CLIENT_ID")
    model = env.get("QUIZ_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", model):
        raise ModelConfigError("QUIZ_MODEL must be a model alias such as gpt-5.4-nano.")

    from azure.identity import ManagedIdentityCredential

    credential = ManagedIdentityCredential(client_id=client_id)

    async def token_provider() -> str:
        # The credential caches tokens itself; fetching happens off the event loop.
        return (await asyncio.to_thread(credential.get_token, scope)).token

    return GatewayModel(base_url, model, token_provider)
