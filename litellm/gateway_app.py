"""Outer transport boundary: credentials never enter LiteLLM request metadata."""

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from entra_auth import (
    HEALTH_ROUTES, INFERENCE_ROUTES, INFERENCE_FIELDS, PROVIDER_OVERRIDES,
)
from credential_context import IncomingCredential, incoming_credential

ASGIApp = Callable[..., Awaitable[None]]


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
        path = scope["path"]
        health = scope["method"] == "GET" and path in HEALTH_ROUTES
        permitted = scope["method"] == "POST" and path in INFERENCE_ROUTES
        if (not health and not permitted) or scope.get("query_string"):
            await self.reject(send, 403, "Route is not permitted.")
            return
        headers = scope.get("headers", [])
        authorization = [value for name, value in headers if name.lower() == b"authorization"]
        token = None
        if len(authorization) == 1:
            scheme, separator, value = authorization[0].partition(b" ")
            if scheme.lower() == b"bearer" and separator and 0 < len(value) <= 32_768:
                try:
                    token = value.decode("ascii")
                except UnicodeDecodeError:
                    token = None
        if not health and token is None:
            await self.reject(send, 401, "A bearer token is required.")
            return
        # Strict forwarding list excludes alternate API keys, baggage and actor spoofing.
        clean_scope = dict(scope)
        clean_scope["headers"] = [
            (name, value) for name, value in headers
            if name.lower() in (b"content-type", b"accept", b"host")
        ]
        if not health:
            clean_scope["headers"].append((b"authorization", b"Bearer redacted"))
            chunks = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunks.extend(message.get("body", b""))
                if len(chunks) > 4_194_304:
                    await self.reject(send, 413, "Inference request is too large.")
                    return
                if not message.get("more_body", False):
                    break
            try:
                body = json.loads(chunks)
            except (ValueError, UnicodeDecodeError):
                await self.reject(send, 400, "Invalid JSON request.")
                return
            if not isinstance(body, dict):
                await self.reject(send, 400, "A JSON object is required.")
                return
            if PROVIDER_OVERRIDES.intersection(body):
                await self.reject(send, 403, "Provider overrides are not permitted.")
                return
            if body.keys() - INFERENCE_FIELDS:
                await self.reject(send, 403, "Unsupported inference options.")
                return
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

            receive = replay
        credential = IncomingCredential(token)
        context = incoming_credential.set(credential)
        try:
            await self.app(clean_scope, receive, send)
        finally:
            credential.value = None
            incoming_credential.reset(context)

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
