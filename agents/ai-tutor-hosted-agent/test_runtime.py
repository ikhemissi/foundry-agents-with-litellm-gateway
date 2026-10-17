import asyncio
import json
import logging
import os
import unittest
from unittest.mock import Mock, patch

import httpx
import jwt
from azure.core.credentials import AccessToken
from openai import APIStatusError, AsyncOpenAI

from main import (
    DelegatedTokenBoundary, application_key, create_agent, create_server,
    RequestCredentialAuth, delegated_key, forwarded_token, prepare_options,
)

CLIENT = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = "22222222-2222-2222-2222-222222222222"


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_playground_options_reach_actual_chat_body_without_include(self) -> None:
        for stream in (False, True):
            for include in ([], ["reasoning.encrypted_content"]):
                with self.subTest(stream=stream, include=include):
                    requests = []

                    async def model(request: httpx.Request) -> httpx.Response:
                        requests.append(json.loads(request.content))
                        if requests[-1].get("stream"):
                            chunk = {
                                "id": "chat-test", "object": "chat.completion.chunk",
                                "created": 1, "model": "gpt-5.4-nano",
                                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"},
                                             "finish_reason": None}],
                            }
                            end = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                            content = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(end)}\n\ndata: [DONE]\n\n"
                            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
                        return httpx.Response(200, json={
                            "id": "chat-test", "object": "chat.completion", "created": 1,
                            "model": "gpt-5.4-nano",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"},
                                         "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        })

                    async def key() -> str:
                        return "legacy-test-key"

                    client = AsyncOpenAI(
                        base_url="https://gateway.example/v1", api_key=key, max_retries=0,
                        http_client=httpx.AsyncClient(transport=httpx.MockTransport(model), follow_redirects=False),
                    )
                    agent = create_agent("https://gateway.example/v1", async_client=client)
                    server = create_server(agent, client, "api_key")
                    async with server.router.lifespan_context(server):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as caller:
                            with self.assertLogs("main", level="WARNING") as logs:
                                response = await caller.post("/responses", json={
                                    "input": "Hello", "include": include, "stream": stream,
                                    "temperature": 0.4, "max_output_tokens": 16,
                                })
                            self.assertNotIn("encrypted_content", " ".join(logs.output))
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertIn("Hello", response.text)
                    self.assertTrue(client.is_closed())
                    self.assertEqual(len(requests), 1)
                    self.assertNotIn("include", requests[0])
                    self.assertEqual(requests[0]["temperature"], 0.4)
                    self.assertEqual(requests[0]["max_completion_tokens"], 16)
                    # The hosting adapter consumes a model stream in both response modes.
                    self.assertTrue(requests[0]["stream"])

    async def test_hook_does_not_drop_arbitrary_options(self) -> None:
        self.assertEqual(prepare_options(Mock(), {"future_option": 42}), {"future_option": 42})

    async def test_concurrent_delegation_is_local_and_headers_are_stripped(self) -> None:
        observations = {}
        pending = []
        ready = asyncio.Event()

        async def app(scope, receive, send):
            self.assertNotIn(b"x-client-litellm-authorization", dict(scope["headers"]))
            observations[scope["path"]] = await delegated_key()
            pending.append(forwarded_token.get())
            if len(observations) == 2:
                ready.set()
            await ready.wait()
            self.assertEqual(await delegated_key(), observations[scope["path"]])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        boundary = DelegatedTokenBoundary(app, "entra_delegated")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary), base_url="http://test") as client:
            await asyncio.gather(*[
                client.post(path, headers={"x-client-litellm-authorization": f"Bearer {token}"})
                for path, token in (("/one", "user-one"), ("/two", "user-two"))
            ])
        self.assertEqual(set(observations.values()), {"user-one", "user-two"})
        self.assertTrue(all(not token.active and not token.value for token in pending))
        with self.assertRaisesRegex(RuntimeError, "fallback"):
            await delegated_key()

    async def test_missing_delegated_token_and_cancellation_never_fallback(self) -> None:
        entered = asyncio.Event()
        captured = []

        async def app(scope, receive, send):
            captured.append(forwarded_token.get())
            entered.set()
            await asyncio.Event().wait()

        boundary = DelegatedTokenBoundary(app, "entra_delegated")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary), base_url="http://test") as client:
            denied = await client.post("/responses")
            self.assertEqual(denied.status_code, 401)
            self.assertFalse(captured)
            task = asyncio.create_task(client.post("/responses", headers={"x-client-litellm-authorization": "Bearer cancel-me"}))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(captured[0].active)
        self.assertEqual(captured[0].value, "")

    async def test_shared_openai_client_does_not_retain_or_mix_user_tokens(self) -> None:
        outgoing = []
        barrier = asyncio.Event()

        async def model(request):
            outgoing.append((json.loads(request.content)["user"], request.headers["Authorization"]))
            if len(outgoing) == 2:
                barrier.set()
            await barrier.wait()
            return httpx.Response(200, json={
                "id": "local", "object": "chat.completion", "created": 1, "model": "test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
            })

        client = AsyncOpenAI(
            base_url="https://gateway.example/v1", api_key="request-local-credential",
            max_retries=0, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(model), follow_redirects=False,
                auth=RequestCredentialAuth(delegated_key),
            ),
        )

        async def app(scope, receive, send):
            await client.chat.completions.create(model="test", messages=[], user=scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async with client:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=DelegatedTokenBoundary(app, "entra_delegated")),
                base_url="http://local",
            ) as caller:
                await asyncio.gather(*[
                    caller.post(path, headers={"x-client-litellm-authorization": f"Bearer {token}"})
                    for path, token in (("/one", "one-secret"), ("/two", "two-secret"))
                ])
                self.assertEqual(client.api_key, "request-local-credential")
        self.assertEqual(dict(outgoing), {"/one": "Bearer one-secret", "/two": "Bearer two-secret"})

    async def test_real_host_forwards_request_token_without_logging_it(self) -> None:
        outgoing = []
        logs = []

        class Capture(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        async def model(request):
            outgoing.append(request.headers["Authorization"])
            chunk = {
                "id": "local", "object": "chat.completion.chunk", "created": 1,
                "model": "gpt-5.4-nano",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"},
                             "finish_reason": "stop"}],
            }
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})

        client = AsyncOpenAI(
            base_url="https://gateway.example/v1", api_key="request-local-credential",
            max_retries=0, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(model), auth=RequestCredentialAuth(delegated_key),
                follow_redirects=False,
            ),
        )
        server = create_server(
            create_agent("https://gateway.example/v1", async_client=client), client, "entra_delegated",
        )
        handler = Capture()
        logging.getLogger().addHandler(handler)
        try:
            async with server.router.lifespan_context(server):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as caller:
                    for stream in (False, True):
                        response = await caller.post("/responses", headers={
                            "Authorization": "Bearer foundry-resource-token-not-for-gateway",
                            "x-client-litellm-authorization": "Bearer delegated-secret-marker",
                        }, json={"input": "Hello", "stream": stream, "store": False})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertIn("Hello", response.text)
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertTrue(client.is_closed())
        self.assertEqual(outgoing, ["Bearer delegated-secret-marker"] * 2)
        self.assertNotIn("delegated-secret-marker", repr(logs))
        self.assertNotIn("foundry-resource-token-not-for-gateway", repr(outgoing))

    async def test_application_credential_rejects_non_agent_default_credentials(self) -> None:
        credential = Mock()
        claims = {"azp": CLIENT, "oid": PRINCIPAL, "idtyp": "app", "xms_act_fct": "11"}
        with patch.dict(os.environ, {
            "ENTRA_AGENT_CLIENT_ID": CLIENT, "ENTRA_AGENT_PRINCIPAL_ID": PRINCIPAL,
            "ENTRA_GATEWAY_SCOPE": f"api://{CLIENT}/.default",
        }):
            credential.get_token.return_value = AccessToken(jwt.encode(claims, "local-test-secret" * 3), 9999999999)
            self.assertEqual(await application_key(credential), credential.get_token.return_value.token)
            for wrong in ({"oid": CLIENT}, {"azp": PRINCIPAL}, {"idtyp": "user"}, {"xms_act_fct": ""}):
                credential.get_token.return_value = AccessToken(jwt.encode({**claims, **wrong}, "local-test-secret" * 3), 9999999999)
                with self.assertRaisesRegex(RuntimeError, "Agent ID"):
                    await application_key(credential)

    async def test_denied_inference_is_not_retried_or_redirected(self) -> None:
        for status in (401, 403, 307):
            calls = []

            async def deny(request):
                calls.append(request)
                return httpx.Response(status, headers={"location": "https://elsewhere.example"})

            async with AsyncOpenAI(
                base_url="https://gateway.example/v1", api_key="delegated-test", max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(deny), follow_redirects=False),
            ) as client:
                with self.assertRaises(APIStatusError):
                    await client.chat.completions.create(model="gpt-5.4-nano", messages=[])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].url.host, "gateway.example")


if __name__ == "__main__":
    unittest.main()
