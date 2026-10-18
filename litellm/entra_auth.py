"""Database-free Entra authorization for inference, MCP and A2A, via LiteLLM's OSS custom_auth hook."""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import httpx
import jwt
from fastapi import HTTPException, Request
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from credential_context import incoming_credential
from gateway_policy import MODEL, NAME, PROVIDER_OVERRIDES, INFERENCE_FIELDS, audit, classify, rpc_method

log = logging.getLogger(__name__)


def guid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected an Entra GUID.")
    return str(UUID(value))


def guid_set(name: str) -> frozenset[str]:
    values = json.loads(os.environ.get(name, "[]"))
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a JSON array.")
    return frozenset(guid(value) for value in values)


class PrincipalDenied(HTTPException):
    """A 403 for a signature-verified principal; carries its identifiers for the audit log."""

    def __init__(self, detail: str, oid: str, azp: str, kind: str, reason: str) -> None:
        super().__init__(403, detail)
        self.oid, self.azp, self.kind, self.reason = oid, azp, kind, reason


@dataclass(frozen=True)
class Capabilities:
    """What an approved application principal may reach beyond inference."""

    mcp: dict[str, frozenset[str]]
    agents: frozenset[str]


def parse_capabilities(raw: str, apps: frozenset[tuple[str, str]]) -> dict[str, Capabilities]:
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("ENTRA_CAPABILITIES must be a JSON object.")
    approved = {oid for oid, _ in apps}
    parsed = {}
    for key, grant in document.items():
        oid = guid(key)
        if oid not in approved or not isinstance(grant, dict) or grant.keys() - {"mcp", "agents"}:
            raise ValueError("ENTRA_CAPABILITIES must reference approved principals with mcp/agents grants.")
        servers = grant.get("mcp", {})
        agents = grant.get("agents", [])
        if not isinstance(servers, dict) or not isinstance(agents, list):
            raise ValueError("Invalid ENTRA_CAPABILITIES grant.")
        mcp = {}
        for server, tools in servers.items():
            if not NAME.match(server) or not isinstance(tools, list) or not tools:
                raise ValueError("Invalid MCP grant.")
            if not all(isinstance(tool, str) and re.match(r"^[A-Za-z0-9_.:-]{1,128}$", tool) for tool in tools):
                raise ValueError("Invalid MCP tool name.")
            mcp[server] = frozenset(tools)
        if not all(isinstance(agent, str) and NAME.match(agent) for agent in agents):
            raise ValueError("Invalid agent name.")
        parsed[oid] = Capabilities(mcp=mcp, agents=frozenset(agents))
    return parsed


class EntraVerifier:
    def __init__(self) -> None:
        self.tenant = guid(os.environ["ENTRA_TENANT_ID"])
        self.audience = guid(os.environ["ENTRA_GATEWAY_CLIENT_ID"])
        self.issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        # Never use a token-provided jku, x5u, issuer, or discovery URL.
        self.jwks_url = f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys"
        principals = json.loads(os.environ.get("ENTRA_APP_PRINCIPALS", "[]"))
        if not isinstance(principals, list):
            raise ValueError("ENTRA_APP_PRINCIPALS must be a JSON array.")
        self.apps = frozenset((guid(p["oid"]), guid(p["azp"])) for p in principals)
        self.capabilities = parse_capabilities(os.environ.get("ENTRA_CAPABILITIES", "{}"), self.apps)
        self.users = guid_set("ENTRA_DELEGATED_USERS")
        self.actors = guid_set("ENTRA_DELEGATED_ACTORS")
        delegated_only = os.environ.get("ENTRA_DELEGATED_ONLY", "false")
        if delegated_only not in ("true", "false"):
            raise ValueError("ENTRA_DELEGATED_ONLY must be true or false.")
        self.delegated_only = delegated_only == "true"
        self.keys: dict[str, jwt.PyJWK] = {}
        self.refreshed_at = float("-inf")
        self.lock = asyncio.Lock()

    async def signing_key(self, kid: str) -> jwt.PyJWK:
        async with self.lock:
            age = time.monotonic() - self.refreshed_at
            if age >= 300 or (kid not in self.keys and age >= 30):
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(5, connect=2), follow_redirects=False,
                    ) as client:
                        response = await client.get(self.jwks_url)
                        response.raise_for_status()
                    if len(response.content) > 1_000_000:
                        raise ValueError("Oversized JWKS.")
                    document = response.json()
                    keys = document["keys"]
                    if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
                        raise ValueError("Invalid JWKS.")
                    self.keys = {
                        key["kid"]: jwt.PyJWK.from_dict(key, algorithm="RS256")
                        for key in keys
                        if key.get("kty") == "RSA"
                        and key.get("use", "sig") == "sig"
                        and key.get("alg", "RS256") == "RS256"
                    }
                    self.refreshed_at = time.monotonic()
                except (httpx.HTTPError, ValueError, TypeError, KeyError, jwt.PyJWTError):
                    log.warning("Entra signing-key service unavailable or invalid.")
                    raise HTTPException(503, "Identity verification unavailable.") from None
            if kid not in self.keys:
                raise HTTPException(401, "Invalid bearer token.")
            return self.keys[kid]

    async def authenticate(self, token: str) -> tuple[str, str, str]:
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not 1 <= len(kid) <= 200:
                raise jwt.InvalidTokenError()
            key = await self.signing_key(kid)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.audience,
                issuer=self.issuer, leeway=0,
                options={
                    "require": ["exp", "nbf", "iat", "iss", "aud", "tid", "oid", "azp", "sub", "ver"],
                    "strict_aud": True,
                },
            )
            oid, actor = guid(claims["oid"]), guid(claims["azp"])
            if claims["tid"] != self.tenant or claims["ver"] != "2.0" or not claims["sub"]:
                raise jwt.InvalidTokenError()
        except (jwt.PyJWTError, ValueError, TypeError):
            raise HTTPException(401, "Invalid bearer token.") from None

        scope = claims.get("scp")
        if claims.get("idtyp") == "app" and scope in (None, "", "/"):
            if self.delegated_only or (oid, actor) not in self.apps:
                raise PrincipalDenied("Application principal is not permitted.", oid, actor, "application", "principal")
            return oid, actor, "application"
        # Agent user accounts are not approved human delegation.
        if (
            claims.get("idtyp") not in (None, "user")
            or not isinstance(scope, str)
            or "gateway.invoke" not in scope.split()
            or oid not in self.users
            or actor not in self.actors
            or "11" not in str(claims.get("xms_act_fct", "")).split()
            or {"11", "13"}.intersection(str(claims.get("xms_sub_fct", "")).split())
        ):
            raise PrincipalDenied("Delegated principal, actor, or scope is not permitted.", oid, actor, "delegated", "delegation")
        return oid, actor, "delegated"


@lru_cache(maxsize=1)
def verifier() -> EntraVerifier:
    return EntraVerifier()


def tool_permission(oid: str, route, grants: Capabilities | None):
    """Build the LiteLLM object permission for an MCP or A2A route, or raise 403."""
    from litellm.proxy._experimental.mcp_server.mcp_server_manager import global_mcp_server_manager
    from litellm.proxy._types import LiteLLM_ObjectPermissionTable
    from litellm.proxy.agent_endpoints.agent_registry import global_agent_registry

    if grants is None:
        raise HTTPException(403, "Principal has no tool or agent access.")
    if route.kind == "mcp":
        if not grants.mcp or (route.resource is not None and route.resource not in grants.mcp):
            raise HTTPException(403, "MCP server is not permitted.")
        servers = {}
        for name, tools in grants.mcp.items():
            server = global_mcp_server_manager.get_mcp_server_by_name(name)
            if server is None:
                raise HTTPException(403, "MCP server is not permitted.")
            servers[server.server_id] = sorted(tools)
        return LiteLLM_ObjectPermissionTable(
            object_permission_id=f"entra-{oid}", mcp_servers=list(servers), mcp_tool_permissions=servers,
        )
    if route.resource not in grants.agents:
        raise HTTPException(403, "Agent is not permitted.")
    agent = global_agent_registry.get_agent_by_name(route.resource)
    if agent is None:
        raise HTTPException(403, "Agent is not permitted.")
    return LiteLLM_ObjectPermissionTable(object_permission_id=f"entra-{oid}", agents=[agent.agent_id])


async def request_rpc(request: Request, route) -> str | None:
    """The JSON-RPC method of an MCP/A2A request, for the audit log only."""
    if route is None or route.kind in ("inference", "health", "a2a_card"):
        return None
    try:
        return rpc_method(await request.json())
    except ValueError:
        return None


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    # The outer boundary removes credentials before LiteLLM logging/metadata.
    credential = incoming_credential.get()
    if credential is None or credential.value is None:
        raise HTTPException(401, "A bearer token is required.")
    token, credential.value = credential.value, None
    route = classify(request.method, request.url.path)
    where = {"route": route.kind if route else None, "resource": route.resource if route else None}
    try:
        oid, actor, kind = await verifier().authenticate(token)
    except PrincipalDenied as denied:
        audit("deny", denied.reason, kind=denied.kind, oid=denied.oid, azp=denied.azp, **where,
              rpc=await request_rpc(request, route))
        raise
    except HTTPException as error:
        # The token failed validation, so none of its claims are trustworthy enough to log.
        audit("deny", "token" if error.status_code == 401 else "identity_service", **where)
        raise
    finally:
        del token
    who = {"kind": kind, "oid": oid, "azp": actor, **where}
    try:
        if route is None or route.kind == "health":
            raise HTTPException(403, "Route is not permitted.")
        metadata = {"principal_kind": kind, "actor_id": actor}
        permission = None
        if route.kind == "inference":
            body = await request.json()
            if not isinstance(body, dict) or body.get("model") != MODEL:
                raise HTTPException(403, "Model is not permitted.")
            # Inference clients cannot override provider credentials, endpoints, or headers.
            if PROVIDER_OVERRIDES.intersection(body):
                raise HTTPException(403, "Provider overrides are not permitted.")
            if body.keys() - INFERENCE_FIELDS:
                raise HTTPException(403, "Unsupported inference options.")
        else:
            # Tools and agents are granted to approved applications only, never to delegated users.
            if kind != "application":
                raise HTTPException(403, "Delegated principals cannot use tools or agents.")
            permission = tool_permission(oid, route, verifier().capabilities.get(oid))
    except HTTPException as error:
        audit("deny", error.detail, **who, rpc=await request_rpc(request, route))
        raise
    audit("allow", "ok", **who, rpc=await request_rpc(request, route))
    return UserAPIKeyAuth(
        api_key=f"entra:{oid}:{actor}", user_id=oid, end_user_id=oid,
        user_role=LitellmUserRoles.INTERNAL_USER, models=[MODEL],
        object_permission=permission, metadata=metadata,
    )
