import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import jwt
from agent_framework import Agent
from agent_framework.observability import enable_instrumentation
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework_foundry_hosting import HostedResponseRequest, ResponsesHostServer
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.monitor.opentelemetry import configure_azure_monitor
from openai import AsyncOpenAI
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway_client import RequestCredentialAuth, GatewayTools, TOOL_GUIDANCE, gateway_origin

log = logging.getLogger(__name__)
FORWARDING_HEADER = b"x-client-litellm-authorization"


@dataclass(repr=False)
class ForwardedToken:
    value: str
    active: bool = True

    def __repr__(self) -> str:
        return "ForwardedToken(<redacted>)"


forwarded_token: ContextVar[ForwardedToken | None] = ContextVar("forwarded_token", default=None)


def create_openai_client(
    base_url: str, get_key: Callable[[], Awaitable[str]],
) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=base_url, api_key="request-local-credential", max_retries=0,
        http_client=httpx.AsyncClient(
            follow_redirects=False,
            auth=RequestCredentialAuth(get_key, gateway_origin(base_url)),
        ),
    )


class DelegatedTokenBoundary:
    def __init__(self, app: ASGIApp, mode: str) -> None:
        self.app, self.mode = app, mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        values = [v for k, v in headers if k.lower() == FORWARDING_HEADER]
        clean_scope = dict(scope)
        clean_scope["headers"] = [(k, v) for k, v in headers if k.lower() != FORWARDING_HEADER]
        token = None
        if self.mode == "entra_delegated" and len(values) == 1:
            scheme, separator, value = values[0].partition(b" ")
            if scheme.lower() == b"bearer" and separator and 0 < len(value) <= 32_768:
                try:
                    token = ForwardedToken(value.decode("ascii"))
                except UnicodeDecodeError:
                    token = None
        if self.mode == "entra_delegated" and scope["method"] == "POST" and token is None:
            await send({
                "type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b'{"error":"Delegated gateway token required."}'})
            return
        context = forwarded_token.set(token)
        try:
            await self.app(clean_scope, receive, send)
        finally:
            if token is not None:
                token.active, token.value = False, ""
            forwarded_token.reset(context)


def prepare_options(
    request: HostedResponseRequest, options: dict[str, Any],
) -> dict[str, Any]:
    if forwarded_token.get() is not None and request.request.get("background"):
        raise ValueError("Delegated inference must run in the foreground of its authenticated request.")
    if "include" in options:
        options.pop("include")
        log.warning("Responses include projections are unsupported by Chat Completions.")
    return options


def auth_mode() -> str:
    # No default: the legacy API-key mode must be chosen explicitly.
    mode = os.environ.get("LITELLM_AUTH_MODE")
    if mode not in ("api_key", "entra", "entra_delegated"):
        raise ValueError("LITELLM_AUTH_MODE must be set to api_key, entra, or entra_delegated.")
    return mode


async def delegated_key() -> str:
    token = forwarded_token.get()
    if token is None or not token.active or not token.value:
        raise RuntimeError("No active delegated gateway token; credential fallback is prohibited.")
    return token.value


async def application_key(credential: DefaultAzureCredential) -> str:
    client_id = str(UUID(os.environ["ENTRA_AGENT_CLIENT_ID"]))
    principal_id = str(UUID(os.environ["ENTRA_AGENT_PRINCIPAL_ID"]))
    scope = os.environ["ENTRA_GATEWAY_SCOPE"]
    if not scope.startswith("api://") or not scope.endswith("/.default"):
        raise ValueError("ENTRA_GATEWAY_SCOPE must be api://<gateway-client-id>/.default.")
    UUID(scope.removeprefix("api://").removesuffix("/.default"))
    result = await asyncio.to_thread(credential.get_token, scope)
    # This is a client-side identity consistency guard, NOT token authentication.
    # The gateway verifies the signature, issuer, audience, time and authorization.
    try:
        claims = jwt.decode(result.token, options={"verify_signature": False})
        if (
            claims.get("azp") != client_id or claims.get("oid") != principal_id
            or claims.get("idtyp") != "app"
            or "11" not in str(claims.get("xms_act_fct", "")).split()
        ):
            raise ValueError()
    except (jwt.PyJWTError, ValueError):
        raise RuntimeError("Credential did not return the configured Entra Agent ID.") from None
    return result.token


def create_agent(
    base_url: str,
    definition_path: Path = Path(__file__).with_name("agent.json"),
    *,
    async_client: AsyncOpenAI,
    gateway_tools: GatewayTools | None = None,
) -> Agent:
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.path.rstrip("/") != "/v1"
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("LITELLM_BASE_URL must be an HTTPS LiteLLM /v1 endpoint.")
    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    for field in ("name", "model", "instructions"):
        if not isinstance(definition.get(field), str) or not definition[field].strip():
            raise ValueError(f"agent.json requires a nonempty {field}.")
    client = OpenAIChatCompletionClient(
        model=definition["model"],
        async_client=async_client,
    )
    if gateway_tools is None:
        return Agent(
            client=client,
            name=definition["name"],
            instructions=definition["instructions"],
        )
    return Agent(
        client=client,
        name=definition["name"],
        instructions=definition["instructions"] + TOOL_GUIDANCE,
        tools=gateway_tools.tools,
    )


async def read_gateway_key(vault: SecretClient) -> str:
    # Resolve lazily: the identity receives RBAC after the hosted version exists.
    secret = await asyncio.to_thread(vault.get_secret, "litellm-master-key")
    if not secret.value:
        raise RuntimeError("Key Vault returned an empty litellm-master-key.")
    return secret.value


def gateway_tools_enabled(mode: str) -> bool:
    setting = os.environ.get("GATEWAY_TOOLS", "disabled")
    if setting not in ("enabled", "disabled"):
        raise ValueError("GATEWAY_TOOLS must be enabled or disabled.")
    if setting == "enabled" and mode != "entra":
        # Delegated users and the legacy key have no tool or agent grants at the gateway.
        raise ValueError("GATEWAY_TOOLS=enabled requires LITELLM_AUTH_MODE=entra.")
    return setting == "enabled"


def create_server(
    agent: Agent, client: AsyncOpenAI, mode: str, gateway_tools: GatewayTools | None = None,
) -> ResponsesHostServer:
    async def prepare(request: HostedResponseRequest, options: dict[str, Any]) -> dict[str, Any]:
        if gateway_tools is not None:
            await gateway_tools.ensure_connected()
        return prepare_options(request, options)

    server = ResponsesHostServer(
        agent, configure_observability=None,
        prepare_options=prepare if gateway_tools is not None else prepare_options,
    )
    server.add_middleware(DelegatedTokenBoundary, mode=mode)
    previous_lifespan = server.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncGenerator[None]:
        try:
            async with previous_lifespan(app):
                yield
        finally:
            if gateway_tools is not None:
                await gateway_tools.aclose()
            await client.close()

    server.router.lifespan_context = lifespan
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"] = (
        "authorization,api-key,x-client-.*,.*token.*,.*secret.*"
    )
    mode = auth_mode()
    tools_enabled = gateway_tools_enabled(mode)
    with ExitStack() as stack:
        credential = stack.enter_context(DefaultAzureCredential())
        configure_azure_monitor(
            connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
            credential=credential,
            enable_live_metrics=False,
        )
        enable_instrumentation(enable_sensitive_data=False, enable_message_events=False)

        if mode == "api_key":
            vault = stack.enter_context(SecretClient(
                vault_url=f"https://{os.environ['AZURE_KEY_VAULT_NAME']}.vault.azure.net",
                credential=credential,
            ))

            async def get_credential() -> str:
                return await read_gateway_key(vault)
        elif mode == "entra":
            async def get_credential() -> str:
                return await application_key(credential)
        else:
            get_credential = delegated_key
        client = create_openai_client(os.environ["LITELLM_BASE_URL"], get_credential)
        gateway_tools = (
            GatewayTools(os.environ["LITELLM_BASE_URL"], get_credential) if tools_enabled else None
        )
        try:
            agent = create_agent(
                os.environ["LITELLM_BASE_URL"], async_client=client, gateway_tools=gateway_tools,
            )
            # Keep the explicit Entra-authenticated exporter; do not configure it twice.
            create_server(agent, client, mode, gateway_tools).run()
        finally:
            if gateway_tools is not None and not gateway_tools.client.is_closed:
                asyncio.run(gateway_tools.aclose())
            if not client.is_closed():
                asyncio.run(client.close())


if __name__ == "__main__":
    main()
