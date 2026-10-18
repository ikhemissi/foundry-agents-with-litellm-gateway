import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from openai import AsyncOpenAI

import gateway_client
from gateway_client import GatewayTools, RequestCredentialAuth, a2a_text
from main import create_agent, create_server, gateway_tools_enabled

ORIGIN = "https://gateway.example"
BASE = f"{ORIGIN}/v1"
MCP_METHODS = {"initialize", "notifications/initialized", "tools/list", "tools/call", "ping"}


async def credential() -> str:
    return "gateway-test-credential"


def text_of(result) -> str:
    return result if isinstance(result, str) else " ".join(str(getattr(item, "text", item)) for item in result)


def fake_gateway():
    """A gateway stand-in: a real MCP server and an A2A endpoint, recording every request."""
    mcp = FastMCP(
        "math", stateless_http=True, streamable_http_path="/mcp/curriculum",
        transport_security=TransportSecuritySettings(allowed_hosts=["gateway.example"]),
    )

    @mcp.tool()
    def get_section(topic_id: str, section_id: str, depth: str = "working") -> dict:
        "Return a section."
        return {"topic_id": topic_id, "section_id": section_id, "depth": depth, "title": "Fractions basics"}

    mcp_app = mcp.streamable_http_app()
    seen = []

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await mcp_app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        body = b""
        if scope["method"] == "POST":
            chunks = []
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            body = b"".join(chunks)
        method = json.loads(body).get("method") if body else None
        seen.append({
            "path": scope["path"], "method": scope["method"], "rpc": method,
            "authorization": headers.get(b"authorization", b"").decode(),
        })
        if scope["path"] == "/a2a/quiz_master":
            request = json.loads(body)
            valid = (
                request.get("method") == "SendMessage"
                and headers.get(b"a2a-version") == b"1.0"
                and request["params"]["message"]["role"] == "ROLE_USER"
            )
            if not valid:
                await send({"type": "http.response.start", "status": 403, "headers": []})
                await send({"type": "http.response.body", "body": b"legacy A2A refused"})
                return
            action = json.loads(request["params"]["message"]["parts"][0]["text"])
            reply = (
                {"quiz_id": "signed-id", "kind": action["kind"], "questions": [{"id": "q1", "prompt": "Simplify 6/8"}]}
                if action["action"] == "create"
                else {"score": 1, "max": 1, "answers_seen": action["answers"]}
            )
            payload = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"message": {
                "messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": json.dumps(reply)}],
            }}}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()  # block like a live connection until the response completes

        await mcp_app(scope, replay, send)

    return app, mcp_app, seen


@asynccontextmanager
async def gateway_session():
    app, mcp_app, seen = fake_gateway()
    async with mcp_app.router.lifespan_context(mcp_app):
        yield app, seen


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_credential_is_attached_only_to_the_bound_origin(self) -> None:
        sent = []

        async def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request.headers.get("authorization"))
            return httpx.Response(200)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False,
            auth=RequestCredentialAuth(credential, ORIGIN),
        ) as client:
            await client.get(f"{ORIGIN}/mcp")
            with self.assertRaisesRegex(RuntimeError, "bound to the gateway origin"):
                await client.get("https://other.example/mcp")
            with self.assertRaises(RuntimeError):
                await client.get("http://gateway.example/mcp")
        self.assertEqual(sent, ["Bearer gateway-test-credential"])

    def test_a2a_text_handles_messages_tasks_and_garbage(self) -> None:
        message = {"result": {"message": {"role": "ROLE_AGENT", "parts": [{"text": "hi"}]}}}
        task = {"result": {"task": {"status": {"message": {"parts": [{"text": "from status"}]}}}}}
        artifacts = {"result": {"task": {"artifacts": [{"parts": [{"text": "from artifact"}]}]}}}
        self.assertEqual(a2a_text(message), "hi")
        self.assertEqual(a2a_text(task), "from status")
        self.assertEqual(a2a_text(artifacts), "from artifact")
        for bad in ({}, {"result": {"message": {"parts": []}}}, {"result": {"kind": "message", "parts": [{"text": "legacy"}]}}, {"error": {"code": -1}}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                a2a_text(bad)


class GatewayToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_and_a2a_1_0_use_the_agent_credential_and_only_allowed_methods(self) -> None:
        async with gateway_session() as (app, seen):
            tools = GatewayTools(BASE, credential, transport=httpx.ASGITransport(app=app))
            self.assertEqual([f.name for f in tools.mcp.functions], [])
            await tools.ensure_connected()
            self.assertEqual([f.name for f in tools.mcp.functions], ["get_section"])
            result = await tools.mcp.functions[0].invoke(arguments={"topic_id": "fractions", "section_id": "fractions-basics"})
            self.assertIn("Fractions basics", text_of(result))
            created = json.loads(text_of(await tools.create_quiz.invoke(
                arguments={"kind": "section", "topic_id": "fractions", "section_id": "fractions-basics"})))
            self.assertEqual(created["quiz_id"], "signed-id")
            graded = json.loads(text_of(await tools.grade_quiz.invoke(
                arguments={"quiz_id": "signed-id", "answers": {"q1": "3/4"}})))
            self.assertEqual(graded["answers_seen"], {"q1": "3/4"})
            await tools.aclose()
        self.assertEqual({entry["authorization"] for entry in seen}, {"Bearer gateway-test-credential"})
        rpc = {entry["rpc"] for entry in seen if entry["path"].startswith("/mcp")}
        self.assertTrue(rpc <= MCP_METHODS, rpc)
        a2a = [entry for entry in seen if entry["path"].startswith("/a2a")]
        self.assertEqual([entry["rpc"] for entry in a2a], ["SendMessage", "SendMessage"])
        self.assertTrue(all(entry["method"] == "POST" for entry in seen))
        self.assertTrue(tools.client.is_closed)

    async def test_bad_tool_arguments_never_reach_the_gateway(self) -> None:
        calls = []

        async def record(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(500)

        tools = GatewayTools(BASE, credential, transport=httpx.MockTransport(record))
        for arguments in (
            {"kind": "bogus", "topic_id": "fractions"}, {"kind": "section", "topic_id": "../x"},
            {"kind": "section", "topic_id": "fractions", "section_id": "Bad Id"},
            {"kind": "section", "topic_id": "fractions", "level": "extreme"},
        ):
            with self.subTest(arguments=arguments):
                reply = json.loads(text_of(await tools._create_quiz(**arguments)))
                self.assertEqual(reply["error"]["code"], "invalid_request")
        for answers in ({}, {f"q{i}": "1" for i in range(11)}, {"q1": "x" * 65}, {"toolongid1": "1"}):
            with self.subTest(answers=answers):
                reply = json.loads(text_of(await tools._grade_quiz("id", answers)))
                self.assertEqual(reply["error"]["code"], "invalid_request")
        self.assertEqual(json.loads(text_of(await tools._grade_quiz("", {"q1": "1"})))["error"]["code"], "invalid_request")
        self.assertEqual(calls, [])
        await tools.aclose()

    async def test_unavailable_gateway_degrades_with_backoff_and_no_response_logging(self) -> None:
        calls = []

        async def failing(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(503, text="sensitive-upstream-body")

        tools = GatewayTools(BASE, credential, transport=httpx.MockTransport(failing))
        with self.assertLogs("gateway_client", level="WARNING") as logs:
            await tools.ensure_connected()
            first = len(calls)
            await tools.ensure_connected()  # within the backoff window: no new network call
        self.assertEqual(len(calls), first)
        self.assertNotIn("sensitive-upstream-body", " ".join(logs.output))
        self.assertNotIn("gateway-test-credential", " ".join(logs.output))
        self.assertEqual([f.name for f in tools.mcp.functions], [])
        with patch.object(gateway_client.time, "monotonic", return_value=1e12):
            await tools.ensure_connected()
        self.assertGreater(len(calls), first)
        reply = json.loads(text_of(await tools._create_quiz("section", "fractions", "fractions-basics")))
        self.assertEqual(reply["error"]["code"], "unavailable")
        await tools.aclose()

    async def test_oversized_or_malformed_a2a_replies_become_a_safe_message(self) -> None:
        for content in (b"x" * 70_000, b"not json", json.dumps({"result": {"message": {"parts": []}}}).encode()):
            with self.subTest(size=len(content)):
                tools = GatewayTools(BASE, credential, transport=httpx.MockTransport(
                    lambda request, content=content: httpx.Response(200, content=content)))
                reply = json.loads(text_of(await tools._create_quiz("section", "fractions", "fractions-basics")))
                self.assertEqual(reply["error"]["code"], "unavailable")
                await tools.aclose()


class EnablementTests(unittest.TestCase):
    def test_tools_are_opt_in_and_entra_only(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(gateway_tools_enabled("api_key"))
            self.assertFalse(gateway_tools_enabled("entra"))
        for mode in ("api_key", "entra_delegated"):
            with patch.dict("os.environ", {"GATEWAY_TOOLS": "enabled"}), self.assertRaisesRegex(ValueError, "entra"):
                gateway_tools_enabled(mode)
        with patch.dict("os.environ", {"GATEWAY_TOOLS": "enabled"}):
            self.assertTrue(gateway_tools_enabled("entra"))
        with patch.dict("os.environ", {"GATEWAY_TOOLS": "maybe"}), self.assertRaises(ValueError):
            gateway_tools_enabled("entra")

    def test_tool_guidance_is_added_only_when_tools_are_enabled(self) -> None:
        with patch("main.OpenAIChatCompletionClient", autospec=True), patch("main.Agent", autospec=True) as agent:
            plain = create_agent(BASE, async_client=AsyncOpenAI(base_url=BASE, api_key="x"))
            self.assertNotIn("tools", agent.call_args.kwargs)
            tools = GatewayTools(BASE, credential)
            create_agent(BASE, async_client=AsyncOpenAI(base_url=BASE, api_key="x"), gateway_tools=tools)
            kwargs = agent.call_args.kwargs
            self.assertEqual(kwargs["tools"], tools.tools)
            self.assertIn("create_quiz", kwargs["instructions"])
            self.assertIn("grade_quiz", kwargs["instructions"])
            self.assertTrue(kwargs["instructions"].startswith(
                json.loads(open("agent.json", encoding="utf-8").read())["instructions"]))
        self.assertIsNotNone(plain)


class HostedRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_tool_calls_reach_mcp_and_a2a_through_the_gateway(self) -> None:
        model_requests = []

        async def model(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            model_requests.append(body)
            roles = [message["role"] for message in body["messages"]]
            if "tool" not in roles:
                calls = [
                    {"id": "call_a", "type": "function", "function": {
                        "name": "get_section", "arguments": json.dumps(
                            {"topic_id": "fractions", "section_id": "fractions-basics"})}},
                    {"id": "call_b", "type": "function", "function": {
                        "name": "create_quiz", "arguments": json.dumps(
                            {"kind": "section", "topic_id": "fractions", "section_id": "fractions-basics"})}},
                ]
                message = {"role": "assistant", "content": None, "tool_calls": calls}
                finish = "tool_calls"
            else:
                tool_text = " ".join(str(m["content"]) for m in body["messages"] if m["role"] == "tool")
                message = {"role": "assistant", "content": f"done: {tool_text}"}
                finish = "stop"
            payload = {"id": "c", "object": "chat.completion", "created": 1, "model": "gpt-5.4-nano",
                       "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-5.4-nano",
                     "choices": [{"index": 0, "delta": {**message, **(
                         {"tool_calls": [{**c, "index": i} for i, c in enumerate(message["tool_calls"])]}
                         if finish == "tool_calls" else {})}, "finish_reason": finish}]}
            if body.get("stream"):
                text = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=payload)

        async with gateway_session() as (gateway, seen):
            tools = GatewayTools(BASE, credential, transport=httpx.ASGITransport(app=gateway))
            client = AsyncOpenAI(
                base_url=BASE, api_key="placeholder", max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(model)),
            )
            agent = create_agent(BASE, async_client=client, gateway_tools=tools)
            server = create_server(agent, client, "entra", tools)
            async with server.router.lifespan_context(server):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://test") as caller:
                    response = await caller.post("/responses", json={"input": "Teach me fractions and quiz me.", "stream": False})
            self.assertEqual(response.status_code, 200, response.text)
        text = response.text
        self.assertIn("done:", text)
        self.assertIn("Fractions basics", text)
        self.assertIn("Simplify 6/8", text)
        self.assertTrue(tools.client.is_closed)
        offered = {tool["function"]["name"] for tool in model_requests[0].get("tools", [])}
        self.assertEqual(offered, {"get_section", "create_quiz", "grade_quiz"})
        self.assertEqual({entry["authorization"] for entry in seen}, {"Bearer gateway-test-credential"})
        self.assertNotIn("gateway-test-credential", json.dumps(model_requests))


if __name__ == "__main__":
    unittest.main()
