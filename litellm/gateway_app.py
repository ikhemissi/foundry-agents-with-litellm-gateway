"""Outer transport boundary: credentials never enter LiteLLM request metadata."""

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from credential_context import IncomingCredential, incoming_credential
from gateway_policy import (
    MAX_BODY_BYTES, a2a_version_ok, audit, classify, forwardable_headers, rpc_method, safe, validate_body,
)

ASGIApp = Callable[..., Awaitable[None]]
# LiteLLM never sees the real credential; the auth hook reads it from the request context.
CREDENTIAL_PLACEHOLDER = b"-"


def bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    authorization = [value for name, value in headers if name.lower() == b"authorization"]
    if len(authorization) != 1:
        return None
    scheme, separator, value = authorization[0].partition(b" ")
    if scheme.lower() != b"bearer" or not separator or not 0 < len(value) <= 32_768:
        return None
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None


class CredentialBoundary:
    def __init__(self, app: ASGIApp, mode: str) -> None:
        if mode not in ("api_key", "entra"):
            raise ValueError("LITELLM_AUTH_MODE must be api_key or entra.")
        self.app, self.mode = app, mode

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: Callable, send: Callable,
    ) -> None:
        if scope["type"] != "http" or self.mode == "api_key":
            await self.app(scope, receive, send)
            return
        route = classify(scope["method"], scope["path"])
        if route is None or scope.get("query_string"):
            audit("deny", "route", method=scope["method"], path=safe(scope["path"], 80),
                  query="yes" if scope.get("query_string") else None)
            await self.reject(send, 403, "Route is not permitted.")
            return
        headers = scope.get("headers", [])
        health = route.kind == "health"
        token = None if health else bearer_token(headers)
        if not health and token is None:
            audit("deny", "no_token", route=route.kind, resource=route.resource)
            await self.reject(send, 401, "A bearer token is required.")
            return
        if route.kind == "a2a" and not a2a_version_ok(headers):
            audit("deny", "a2a_version", route=route.kind, resource=route.resource)
            await self.reject(send, 403, "A2A protocol 1.x is required.")
            return
        clean_scope = dict(scope)
        clean_scope["headers"] = forwardable_headers(route, headers)
        if not health:
            clean_scope["headers"].append((b"authorization", CREDENTIAL_PLACEHOLDER))
        if route.has_body:
            receive = await self.validated_body(route, receive, send)
            if receive is None:
                return
        credential = IncomingCredential(token)
        context = incoming_credential.set(credential)
        try:
            await self.app(clean_scope, receive, send)
        finally:
            credential.value = None
            incoming_credential.reset(context)

    async def validated_body(self, route, receive: Callable, send: Callable) -> Callable | None:
        limit = MAX_BODY_BYTES[route.body_class]
        chunks = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunks.extend(message.get("body", b""))
            if len(chunks) > limit:
                audit("deny", "too_large", route=route.kind, resource=route.resource)
                await self.reject(send, 413, "Request is too large.")
                return None
            if not message.get("more_body", False):
                break
        try:
            body = json.loads(chunks)
        except (ValueError, UnicodeDecodeError):
            audit("deny", "invalid_json", route=route.kind, resource=route.resource)
            await self.reject(send, 400, "Invalid JSON request.")
            return None
        if not isinstance(body, dict):
            audit("deny", "not_object", route=route.kind, resource=route.resource)
            await self.reject(send, 400, "A JSON object is required.")
            return None
        rejection = validate_body(route, body)
        if rejection:
            audit("deny", "body", route=route.kind, resource=route.resource,
                  rpc=rpc_method(body), detail=rejection.replace(" ", "_"))
            await self.reject(send, 403, rejection)
            return None
        if route.kind == "inference":
            # Caller metadata is not a source of identity or trusted telemetry.
            body.pop("user", None)
            body.pop("metadata", None)
        payload = json.dumps(body).encode()
        delivered = False
        original_receive = receive

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return await original_receive()
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}

        return replay

    @staticmethod
    async def reject(send: Callable, status: int, message: str) -> None:
        await send({
            "type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": json.dumps({"error": message}).encode(),
        })
