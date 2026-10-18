"""GatewayModel against a local mock OpenAI-compatible gateway (no sockets: httpx ASGITransport)."""

import asyncio
import json
import logging
import time
import unittest

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from app import create_app
from model import GatewayModel, RequestCredentialAuth, validate_base_url, ModelConfigError

BASE = "https://gateway.test/v1"
KEY = "unit-test-signing-key-0123456789-abcdef"


def completion(content, finish_reason="stop"):
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-5.4-nano",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class MockGateway:
    def __init__(self):
        self.requests = []
        self.behaviour = lambda body: JSONResponse(completion(json.dumps({"ok": True})))
        self.evil_hits = 0
        routes = [
            Route("/v1/chat/completions", self.chat, methods=["POST"]),
            Route("/evil", self.evil, methods=["POST", "GET"]),
        ]
        self.app = Starlette(routes=routes)

    async def chat(self, request):
        body = json.loads(await request.body())
        self.requests.append({"headers": dict(request.headers), "body": body})
        result = self.behaviour(body)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def evil(self, request):
        self.evil_hits += 1
        return JSONResponse({"stolen": request.headers.get("authorization")})


class TokenSource:
    def __init__(self):
        self.count = 0

    async def __call__(self):
        self.count += 1
        return f"secret-token-{self.count}"


class LogCapture:
    def __enter__(self):
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        self.handler, self.root = handler, logging.getLogger()
        self.previous = self.root.level
        self.root.addHandler(handler)
        self.root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.previous)

    @property
    def text(self):
        return "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in self.records)


class GatewayModelTests(unittest.IsolatedAsyncioTestCase):
    def build(self, gateway, tokens=None, **options):
        options.setdefault("cooldown", 0.0)
        model = GatewayModel(BASE, "gpt-5.4-nano", tokens or TokenSource(), transport=httpx.ASGITransport(app=gateway.app), **options)
        self.addAsyncCleanup(model.aclose)
        return model

    async def test_request_shape_and_per_request_bearer_token(self):
        gateway, tokens = MockGateway(), TokenSource()
        model = self.build(gateway, tokens)
        self.assertEqual(await model.complete_json("system text", {"hello": "wörld"}, max_tokens=321), {"ok": True})
        self.assertEqual(await model.complete_json("system text", {"hello": "again"}, max_tokens=321), {"ok": True})
        first, second = gateway.requests
        self.assertEqual(first["headers"]["authorization"], "Bearer secret-token-1")
        self.assertEqual(second["headers"]["authorization"], "Bearer secret-token-2")
        body = first["body"]
        self.assertEqual(body["model"], "gpt-5.4-nano")
        self.assertEqual(body["max_completion_tokens"], 321)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "system text")
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"hello": "wörld"})
        self.assertLessEqual(set(body), {"model", "messages", "max_completion_tokens", "response_format"})
        self.assertFalse({"user", "metadata", "temperature", "tools", "stream"} & set(body))
        # the credential is not kept on the client between requests
        self.assertNotIn("authorization", {k.lower() for k in model._http.headers})

    async def test_bad_responses_degrade_to_none(self):
        gateway = MockGateway()
        model = self.build(gateway)
        cases = {
            "http 500": lambda body: JSONResponse({"error": "boom secret-token-1"}, status_code=500),
            "http 401": lambda body: JSONResponse({"error": "no"}, status_code=401),
            "http 429": lambda body: JSONResponse({"error": "slow down"}, status_code=429),
            "not json": lambda body: JSONResponse(completion("this is not json")),
            "json list": lambda body: JSONResponse(completion("[1, 2]")),
            "json string": lambda body: JSONResponse(completion('"text"')),
            "truncated": lambda body: JSONResponse(completion('{"a":', finish_reason="length")),
            "empty content": lambda body: JSONResponse(completion("")),
            "null content": lambda body: JSONResponse(completion(None)),
            "no choices": lambda body: JSONResponse({"id": "x", "object": "chat.completion", "created": 1, "model": "m", "choices": []}),
            "garbage body": lambda body: httpx.Response(200, text="<html>nope</html>"),
        }
        for name, behaviour in cases.items():
            gateway.behaviour = behaviour
            with LogCapture() as logs:
                self.assertIsNone(await model.complete_json("s", {"a": 1}, max_tokens=50), name)
            self.assertNotIn("secret-token", logs.text, name)
            self.assertNotIn("boom", logs.text, name)

    async def test_redirects_are_not_followed_and_the_token_is_not_forwarded(self):
        gateway = MockGateway()
        gateway.behaviour = lambda body: RedirectResponse("https://gateway.test/evil", status_code=307)
        model = self.build(gateway)
        self.assertIsNone(await model.complete_json("s", {"a": 1}, max_tokens=50))
        self.assertEqual(gateway.evil_hits, 0)
        self.assertEqual(len(gateway.requests), 1)

    async def test_slow_gateway_is_cut_off_and_triggers_a_cooldown(self):
        gateway = MockGateway()

        async def slow(body):
            await asyncio.sleep(5)
            return JSONResponse(completion("{}"))

        gateway.behaviour = slow
        model = self.build(gateway, total_timeout=0.2, cooldown=60.0)
        started = time.monotonic()
        self.assertIsNone(await model.complete_json("s", {"a": 1}, max_tokens=50))
        self.assertLess(time.monotonic() - started, 2)
        # during the cool-down the gateway is not even contacted
        count = len(gateway.requests)
        started = time.monotonic()
        self.assertIsNone(await model.complete_json("s", {"a": 1}, max_tokens=50))
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(len(gateway.requests), count)

    async def test_recovers_once_the_cooldown_has_passed(self):
        gateway = MockGateway()
        gateway.behaviour = lambda body: JSONResponse({}, status_code=503)
        model = self.build(gateway, cooldown=0.1)
        self.assertIsNone(await model.complete_json("s", {}, max_tokens=50))
        self.assertIsNone(await model.complete_json("s", {}, max_tokens=50))  # still cooling down
        self.assertEqual(len(gateway.requests), 1)
        await asyncio.sleep(0.15)
        gateway.behaviour = lambda body: JSONResponse(completion('{"back": true}'))
        self.assertEqual(await model.complete_json("s", {}, max_tokens=50), {"back": True})

    async def test_token_acquisition_failure_is_swallowed_and_never_logged_beyond_the_class(self):
        gateway = MockGateway()

        async def failing_tokens():
            raise PermissionError("managed identity said: super-secret-detail")

        model = self.build(gateway, failing_tokens)
        with LogCapture() as logs:
            self.assertIsNone(await model.complete_json("s", {"a": 1}, max_tokens=50))
        self.assertEqual(gateway.requests, [])
        self.assertNotIn("super-secret-detail", logs.text)

    async def test_tokens_prompts_and_bodies_never_reach_the_logs(self):
        gateway, tokens = MockGateway(), TokenSource()
        model = self.build(gateway, tokens)
        gateway.behaviour = lambda body: JSONResponse(completion('{"answer": "response-body-marker"}'))
        with LogCapture() as logs:
            await model.complete_json("system-marker", {"learner": "prompt-marker"}, max_tokens=50)
            gateway.behaviour = lambda body: JSONResponse({"detail": "error-body-marker"}, status_code=500)
            await model.complete_json("system-marker", {"learner": "prompt-marker"}, max_tokens=50)
        for marker in ("secret-token", "system-marker", "prompt-marker", "response-body-marker", "error-body-marker"):
            self.assertNotIn(marker, logs.text)

    async def test_credential_is_bound_to_the_gateway_origin(self):
        seen = []

        async def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json={})

        tokens = TokenSource()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=RequestCredentialAuth(tokens, BASE)) as client:
            await client.get("https://gateway.test/v1/x")
            with self.assertRaises(RuntimeError):
                await client.get("https://elsewhere.test/v1/x")
        self.assertEqual(seen, ["Bearer secret-token-1"])
        self.assertEqual(tokens.count, 1)

    def test_base_url_validation(self):
        self.assertEqual(validate_base_url("https://gateway.test/v1"), "https://gateway.test/v1")
        self.assertEqual(validate_base_url("https://gateway.test/v1/"), "https://gateway.test/v1/")
        for bad in ("http://gateway.test/v1", "https://gateway.test/", "https://u:p@gateway.test/v1", "https://gateway.test/v1?x=1", "https://gateway.test/v1?"):
            with self.assertRaises(ModelConfigError, msg=bad):
                validate_base_url(bad)


class EndToEndWithGatewayModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_a2a_quiz_flow_uses_the_gateway_and_survives_its_failure(self):
        gateway, tokens = MockGateway(), TokenSource()
        model = GatewayModel(BASE, "gpt-5.4-nano", tokens, transport=httpx.ASGITransport(app=gateway.app), cooldown=0.0)
        app = create_app("http://quiz.test/", model=model, signing_key=KEY)

        def respond(body):
            payload = json.loads(body["messages"][1]["content"])
            if "questions" in payload:
                content = {"prompts": {q["id"]: f"Fun time! {q['prompt']} You can do it." for q in payload["questions"]}}
            else:
                content = {"feedback": {i["id"]: "Good effort: compare your steps with the worked solution." for i in payload["items"]}}
            return JSONResponse(completion(json.dumps(content)))

        gateway.behaviour = respond

        def rpc(text):
            return {"jsonrpc": "2.0", "id": "r1", "method": "SendMessage", "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": text}]}}}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://quiz.test", headers={"A2A-Version": "1.0"}) as http:
            async def send(payload):
                response = await http.post("/", json=rpc(json.dumps(payload)))
                return json.loads(response.json()["result"]["message"]["parts"][0]["text"])

            quiz = await send({"action": "create", "kind": "section", "topic_id": "primes", "section_id": "primes-gcd-lcm", "count": 4})
            self.assertIs(quiz["model_assisted"], True)
            self.assertTrue(all(q["prompt"].startswith("Fun time!") for q in quiz["questions"]))
            graded = await send({"action": "grade", "quiz_id": quiz["quiz_id"], "answers": {f"q{i}": "wrong" for i in range(1, 5)}})
            self.assertEqual((graded["score"], graded["passed"], graded["model_assisted"]), (0, False, True))
            self.assertTrue(all("feedback" in r for r in graded["results"]))

            gateway.behaviour = lambda body: JSONResponse({"error": "down"}, status_code=502)
            again = await send({"action": "create", "kind": "section", "topic_id": "primes", "section_id": "primes-gcd-lcm", "count": 4})
            self.assertIs(again["model_assisted"], False)
            self.assertEqual(len(again["questions"]), 4)
            regrade = await send({"action": "grade", "quiz_id": quiz["quiz_id"], "answers": {f"q{i}": "wrong" for i in range(1, 5)}})
            self.assertEqual((regrade["score"], regrade["model_assisted"]), (0, False))
        self.assertTrue(all(r["headers"]["authorization"].startswith("Bearer secret-token-") for r in gateway.requests))
        await model.aclose()


if __name__ == "__main__":
    unittest.main()
