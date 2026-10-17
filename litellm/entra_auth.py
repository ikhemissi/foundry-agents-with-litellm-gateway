"""Database-free inference policy, invoked through LiteLLM's OSS custom_auth hook."""

import asyncio
import json
import logging
import os
import time
from functools import lru_cache
from uuid import UUID

import httpx
import jwt
from fastapi import HTTPException, Request
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from credential_context import incoming_credential

log = logging.getLogger(__name__)
INFERENCE_ROUTES = frozenset({
    "/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses",
})
HEALTH_ROUTES = frozenset({"/health/liveliness", "/health/readiness"})
MODEL = "gpt-5.4-nano"
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


def guid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected an Entra GUID.")
    return str(UUID(value))


def guid_set(name: str) -> frozenset[str]:
    values = json.loads(os.environ.get(name, "[]"))
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a JSON array.")
    return frozenset(guid(value) for value in values)


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
                raise HTTPException(403, "Application principal is not permitted.")
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
            raise HTTPException(403, "Delegated principal, actor, or scope is not permitted.")
        return oid, actor, "delegated"


@lru_cache(maxsize=1)
def verifier() -> EntraVerifier:
    return EntraVerifier()


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    # The outer boundary removes credentials before LiteLLM logging/metadata.
    credential = incoming_credential.get()
    if credential is None or credential.value is None:
        raise HTTPException(401, "A bearer token is required.")
    token, credential.value = credential.value, None
    oid, actor, kind = await verifier().authenticate(token)
    del token
    if request.method != "POST" or request.url.path not in INFERENCE_ROUTES:
        raise HTTPException(403, "Only inference routes are permitted.")
    body = await request.json()
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise HTTPException(403, "Model is not permitted.")
    # Inference clients cannot override provider credentials, endpoints, or headers.
    if PROVIDER_OVERRIDES.intersection(body):
        raise HTTPException(403, "Provider overrides are not permitted.")
    if body.keys() - INFERENCE_FIELDS:
        raise HTTPException(403, "Unsupported inference options.")
    return UserAPIKeyAuth(
        api_key=f"entra:{oid}:{actor}", user_id=oid, end_user_id=oid,
        user_role=LitellmUserRoles.INTERNAL_USER, models=[MODEL],
        metadata={"principal_kind": kind, "actor_id": actor},
    )
