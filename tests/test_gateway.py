"""Local RSA/JWKS and real pinned LiteLLM HTTP-stack tests; no Azure calls."""

import importlib.metadata
import json
import logging
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_TELEMETRY"] = "False"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "litellm"))

import httpx
import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from litellm import ModelResponse, Router
from litellm.proxy import proxy_server as proxy
from litellm.proxy.types_utils.utils import get_instance_fn

from credential_context import incoming_credential
from entra_auth import verifier
from gateway_app import CredentialBoundary
from serve import main as serve_main

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
APP = "33333333-3333-3333-3333-333333333333"
PRINCIPAL = "44444444-4444-4444-4444-444444444444"
USER = "55555555-5555-5555-5555-555555555555"
ACTOR = "66666666-6666-6666-6666-666666666666"
OTHER = "77777777-7777-7777-7777-777777777777"
MODEL = "gpt-5.4-nano"


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = jwt.PyJWK.from_dict({
            **jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key(), as_dict=True),
            "kid": "local-key", "alg": "RS256", "use": "sig",
        })

    async def asyncSetUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "ENTRA_TENANT_ID": TENANT, "ENTRA_GATEWAY_CLIENT_ID": AUDIENCE,
            "ENTRA_APP_PRINCIPALS": json.dumps([{"oid": PRINCIPAL, "azp": APP}]),
            "ENTRA_DELEGATED_USERS": json.dumps([USER]),
            "ENTRA_DELEGATED_ACTORS": json.dumps([ACTOR]),
            "ENTRA_DELEGATED_ONLY": "false",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        verifier.cache_clear()
        self.config_path = Path(__file__).resolve().parents[1] / "litellm" / "config.entra.yaml"
        self.hook = get_instance_fn(value="entra_auth.user_api_key_auth", config_file_path=str(self.config_path))
        self.verifier = self.hook.__globals__["verifier"]()
        self.verifier.keys = {"local-key": self.jwk}
        self.verifier.refreshed_at = time.monotonic()
        self.router = Router(model_list=[{
            "model_name": MODEL,
            "litellm_params": {"model": "openai/local-test", "api_key": "mock-provider-key"},
        }])
        self.inference = AsyncMock(return_value=ModelResponse(
            id="chat-local", model=MODEL,
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Local answer"},
                      "finish_reason": "stop"}],
        ))
        self.router.acompletion = self.inference
        self.patches = [
            patch.object(proxy, "llm_router", self.router),
            patch.object(proxy, "llm_model_list", self.router.model_list),
            patch.object(proxy, "user_custom_auth", self.hook),
            patch.object(proxy, "master_key", "sk-local-master-key-" + "a" * 40),
            patch.object(proxy, "general_settings", {
                "custom_auth": "entra_auth.user_api_key_auth",
                "disable_spend_logs": True, "custom_auth_run_common_checks": False,
            }),
            patch.object(proxy, "premium_user", False),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=CredentialBoundary(proxy.app, "entra")),
            base_url="http://local",
        )
        self.addAsyncCleanup(self.client.aclose)

    def token(self, changes=None, *, delegated=False, key=None, algorithm="RS256") -> str:
        now = int(time.time())
        claims = {
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUDIENCE,
            "tid": TENANT, "oid": PRINCIPAL, "azp": APP, "sub": PRINCIPAL, "ver": "2.0",
            "idtyp": "app", "iat": now - 1, "nbf": now - 1, "exp": now + 300,
        }
        if delegated:
            claims.update(oid=USER, azp=ACTOR, sub=USER, idtyp="user",
                          scp="gateway.invoke", xms_act_fct="11")
        claims.update(changes or {})
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key or self.private_key, algorithm=algorithm, headers={"kid": "local-key"})

    async def post(self, token=None, *, path="/v1/chat/completions", body=None):
        return await self.client.post(
            path, headers={"Authorization": f"Bearer {token}"} if token else {},
            json=body or {"model": MODEL, "messages": [{"role": "user", "content": "Hello"}]},
        )

    async def test_pinned_runtime_app_and_delegated_success(self) -> None:
        self.assertEqual(importlib.metadata.version("litellm"), "1.105.1")
        for delegated in (False, True):
            response = await self.post(self.token(delegated=delegated))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "Local answer")
        self.assertEqual(self.inference.await_count, 2)

    async def test_launcher_and_documented_custom_hook_are_wired(self) -> None:
        from litellm.proxy.types_utils.utils import get_instance_fn

        config_path = Path(__file__).resolve().parents[1] / "litellm" / "config.entra.yaml"
        config = yaml.safe_load(config_path.read_text())
        settings = config["general_settings"]
        hook = get_instance_fn(value=settings["custom_auth"], config_file_path=str(config_path))
        self.assertIs(hook.__globals__["incoming_credential"], incoming_credential)
        self.assertNotIn("enable_jwt_auth", settings)
        self.assertNotIn("custom_auth_settings", settings)
        self.assertTrue(settings["disable_spend_logs"])
        with patch.dict(os.environ, {"LITELLM_AUTH_MODE": "entra"}), patch("serve.uvicorn.run") as run:
            serve_main()
            boundary = run.call_args.args[0]
            self.assertIsInstance(boundary, CredentialBoundary)
            self.assertIs(boundary.app, proxy.app)
            self.assertEqual(boundary.mode, "entra")
            self.assertEqual(Path(os.environ["CONFIG_FILE_PATH"]), config_path)

    async def test_invalid_tokens_reject_without_upstream_or_key_fallback(self) -> None:
        tokens = [
            None, "not-a-jwt", "sk-local-master-key-" + "a" * 40,
            self.token({"exp": int(time.time()) - 1}),
            self.token({"nbf": int(time.time()) + 300}),
            self.token({"iss": "https://invalid.example/v2.0"}),
            self.token({"aud": f"api://{AUDIENCE}"}),
            self.token({"tid": OTHER}), self.token({"oid": None}),
            self.token(key=self.wrong_key), self.token(key="x" * 64, algorithm="HS256"),
            self.token({"ver": "1.0"}),
        ]
        for token in tokens:
            with self.subTest(case=tokens.index(token)):
                response = await self.post(token)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertNotIn(token or "no-secret-marker", response.text)
        self.inference.assert_not_awaited()

    async def test_authorization_claims_routes_and_models(self) -> None:
        tokens = [
            self.token({"oid": OTHER}), self.token({"azp": OTHER}),
            self.token({"idtyp": None}),
            self.token({"oid": OTHER}, delegated=True),
            self.token({"azp": OTHER}, delegated=True),
            self.token({"scp": "not-gateway.invoke"}, delegated=True),
            self.token({"xms_act_fct": ""}, delegated=True),
            self.token({"xms_sub_fct": "13"}, delegated=True),
            self.token({"idtyp": "app"}, delegated=True),
        ]
        for token in tokens:
            response = await self.post(token)
            self.assertEqual(response.status_code, 403, response.text)
        self.verifier.delegated_only = True
        self.assertEqual((await self.post(self.token())).status_code, 403)
        self.verifier.delegated_only = False
        for path in ("/key/new", "/models", "/health", "/health/services", "/health/readiness/"):
            response = await self.post(self.token(), path=path)
            self.assertEqual(response.status_code, 403, response.text)
        for body in (
            {"model": "other"}, {"model": MODEL, "api_base": "http://evil"},
            {"model": MODEL, "extra_headers": {"Authorization": "secret"}},
            {"model": MODEL, "model_list": []}, {"model": MODEL, "fallbacks": ["other"]},
        ):
            body["messages"] = [{"role": "user", "content": "Hello"}]
            self.assertEqual((await self.post(self.token(), body=body)).status_code, 403)
        self.inference.assert_not_awaited()

    async def test_exact_unauthenticated_probes_only(self) -> None:
        for path in ("/health/liveliness", "/health/readiness"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
        for path in ("/health", "/health/services", "/health/readiness/", "/health/readiness?x=1", "/"):
            self.assertEqual((await self.client.get(path)).status_code, 403)
        self.inference.assert_not_awaited()

    async def test_credentials_removed_before_inner_logs_and_metadata(self) -> None:
        captured = []

        async def inner(scope, receive, send):
            captured.append(scope)
            self.assertNotIn("secret-raw", repr(scope))
            self.assertNotIn("secret-raw", repr(incoming_credential.get()))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=CredentialBoundary(inner, "entra")),
            base_url="http://local",
        ) as client:
            response = await client.post("/v1/chat/completions", headers={
                "Authorization": "Bearer secret-raw", "x-api-key": "secret-raw",
                "baggage": "secret-raw", "x-client-litellm-authorization": "Bearer secret-raw",
            }, json={"model": MODEL})
            self.assertEqual(response.status_code, 200)
        self.assertIsNone(incoming_credential.get())
        self.assertNotIn("secret-raw", repr(captured))

    async def test_real_proxy_does_not_log_or_forward_tokens_or_caller_identity(self) -> None:
        token = self.token()
        messages = []

        class Capture(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger("LiteLLM Proxy")
        logger.addHandler(handler)
        try:
            response = await self.post(token, body={
                "model": MODEL, "messages": [{"role": "user", "content": "Hello"}],
                "user": "spoofed-human", "metadata": {"credential": token},
            })
            self.assertEqual(response.status_code, 200, response.text)
            denied = await self.post(self.token({"oid": OTHER}))
            self.assertEqual(denied.status_code, 403)
        finally:
            logger.removeHandler(handler)
        observed = repr(self.inference.call_args)
        self.assertNotIn(token, observed)
        self.assertNotIn("spoofed-human", observed)
        self.assertNotIn(token, repr(messages))
        self.assertIn(PRINCIPAL, observed)

    async def test_jwks_cache_rotation_trust_and_timeout(self) -> None:
        self.verifier.keys = {}
        self.verifier.refreshed_at = float("-inf")
        calls = []
        original = httpx.AsyncClient

        async def jwks(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"keys": [self.jwk._jwk_data]})

        with patch("entra_auth.httpx.AsyncClient", side_effect=lambda **kw: original(
            transport=httpx.MockTransport(jwks), **kw,
        )) as factory:
            await self.verifier.authenticate(self.token())
            await self.verifier.authenticate(self.token())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys")
            self.assertFalse(factory.call_args.kwargs["follow_redirects"])
            self.assertEqual(factory.call_args.kwargs["timeout"].read, 5)
            self.verifier.refreshed_at -= 301
            await self.verifier.authenticate(self.token())
            self.assertEqual(len(calls), 2)

    async def test_jwks_unavailable_and_unknown_signing_key_fail_closed(self) -> None:
        original = httpx.AsyncClient

        async def unavailable(request):
            raise httpx.ReadTimeout("mock timeout", request=request)

        self.verifier.refreshed_at = float("-inf")
        with patch("entra_auth.httpx.AsyncClient", side_effect=lambda **kw: original(
            transport=httpx.MockTransport(unavailable), **kw,
        )):
            response = await self.post(self.token())
            self.assertEqual(response.status_code, 503, response.text)
        self.inference.assert_not_awaited()

    async def test_unknown_kid_refreshes_after_cooldown_and_rotation_is_accepted(self) -> None:
        original = httpx.AsyncClient
        rotated = {
            **jwt.algorithms.RSAAlgorithm.to_jwk(self.wrong_key.public_key(), as_dict=True),
            "kid": "rotated-key", "alg": "RS256", "use": "sig",
        }
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        token = jwt.encode(claims, self.wrong_key, algorithm="RS256", headers={
            "kid": "rotated-key", "jku": "https://attacker.invalid/keys",
        })
        first = await self.post(token)
        self.assertEqual(first.status_code, 401)
        calls = []

        async def keys(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"keys": [rotated]})

        self.verifier.refreshed_at -= 31
        with patch("entra_auth.httpx.AsyncClient", side_effect=lambda **kw: original(
            transport=httpx.MockTransport(keys), **kw,
        )):
            response = await self.post(token)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, [self.verifier.jwks_url])

    async def test_legacy_mode_uses_native_key_auth(self) -> None:
        with patch.object(proxy, "user_custom_auth", None), patch.object(proxy, "general_settings", {}):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=CredentialBoundary(proxy.app, "api_key")),
                base_url="http://local",
            ) as client:
                denied = await client.post("/v1/chat/completions", json={"model": MODEL, "messages": []})
                self.assertEqual(denied.status_code, 401)
                response = await client.post("/v1/chat/completions", headers={
                    "Authorization": "Bearer sk-local-master-key-" + "a" * 40,
                }, json={"model": MODEL, "messages": []})
                self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
