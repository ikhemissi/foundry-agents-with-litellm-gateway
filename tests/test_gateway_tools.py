"""Real pinned LiteLLM + our boundary + the real demo MCP server and A2A agent, in a separate process.

Run with the gateway test environment (tests/requirements.gateway.txt plus a2a-sdk). The process
mirrors production: gateway files beside the generated config, hook loaded by LiteLLM's loader.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import httpx2
import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPO = Path(__file__).resolve().parents[1]
TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
HOSTED = ("aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000001")
LIMITED = ("aaaaaaaa-0000-0000-0000-000000000002", "bbbbbbbb-0000-0000-0000-000000000002")
NOGRANT = ("aaaaaaaa-0000-0000-0000-000000000003", "bbbbbbbb-0000-0000-0000-000000000003")
UNAPPROVED = ("aaaaaaaa-0000-0000-0000-000000000004", "bbbbbbbb-0000-0000-0000-000000000004")
QUIZ = ("aaaaaaaa-0000-0000-0000-000000000005", "bbbbbbbb-0000-0000-0000-000000000005")
USER = "55555555-5555-5555-5555-555555555555"
ACTOR = "66666666-6666-6666-6666-666666666666"

state: dict = {}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def setUpModule() -> None:
    root = Path(tempfile.mkdtemp(prefix="gateway-tools-"))
    state["root"] = root
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state["key"] = key
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    (root / "jwk.json").write_text(json.dumps({**jwk, "kid": "fixture-key", "alg": "RS256", "use": "sig"}))
    for path in (REPO / "litellm").glob("*.py"):
        shutil.copy(path, root / path.name)
    ports = {"mcp": free_port(), "a2a": free_port(), "proxy": free_port(), "provider": free_port()}
    from cryptography.hazmat.primitives import serialization

    (root / "key.pem").write_text(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    state["ports"] = ports

    config = yaml.safe_load((REPO / "litellm" / "config.entra.yaml").read_text())
    config["model_list"] = [{
        "model_name": "gpt-5.4-nano",
        "litellm_params": {
            "model": "openai/local-test", "api_key": "mock-provider-key",
            "api_base": f"http://127.0.0.1:{ports['provider']}",
        },
    }]
    config["litellm_settings"].pop("callbacks", None)  # no telemetry exporter in the fixture
    overlay = (REPO / "litellm" / "config.demo.yaml").read_text()
    overlay = overlay.replace("http://demo-mcp/mcp", f"http://127.0.0.1:{ports['mcp']}/mcp")
    overlay = overlay.replace("http://demo-a2a/", f"http://127.0.0.1:{ports['a2a']}/")
    config.update(yaml.safe_load(overlay))
    (root / "config.yaml").write_text(yaml.safe_dump(config))

    env = {
        **os.environ,
        "FIXTURE_DIR": str(root), "REPO_ROOT": str(REPO), "FIXTURE_PORTS": json.dumps(ports),
        "PYTHONPATH": str(root), "LITELLM_AUTH_MODE": "entra",
        "ENTRA_TENANT_ID": TENANT, "ENTRA_GATEWAY_CLIENT_ID": AUDIENCE,
        "ENTRA_APP_PRINCIPALS": json.dumps([{"oid": o, "azp": a} for o, a in (HOSTED, LIMITED, NOGRANT, QUIZ)]),
        "FIXTURE_QUIZ_OID": QUIZ[0], "FIXTURE_QUIZ_AZP": QUIZ[1],
        "ENTRA_DELEGATED_USERS": json.dumps([USER]), "ENTRA_DELEGATED_ACTORS": json.dumps([ACTOR]),
        "ENTRA_CAPABILITIES": json.dumps({
            HOSTED[0]: {"mcp": {"curriculum": ["list_topics", "get_topic", "get_section", "list_learning_modes"]},
                        "agents": ["quiz_master"]},
            LIMITED[0]: {"mcp": {"curriculum": ["list_topics"]}},
        }),
        "AZURE_API_BASE": "https://unused.example",
    }
    log = open(root / "fixture.log", "wb")
    state["log"] = log
    state["process"] = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "support" / "gateway_tools_fixture.py")],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=root,
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        if (root / "ready").exists():
            return
        if state["process"].poll() is not None:
            break
        time.sleep(0.3)
    tearDownModule()
    raise RuntimeError("Fixture did not start:\n" + (root / "fixture.log").read_text()[-3000:])


def tearDownModule() -> None:
    process = state.get("process")
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
    if state.get("log"):
        state["log"].close()
    shutil.rmtree(state.get("root", ""), ignore_errors=True)


def token(principal, *, delegated: bool = False) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUDIENCE, "tid": TENANT,
        "oid": principal[0], "azp": principal[1], "sub": principal[0], "ver": "2.0", "idtyp": "app",
        "iat": now - 1, "nbf": now - 1, "exp": now + 600,
    }
    if delegated:
        claims.update(oid=USER, azp=ACTOR, sub=USER, idtyp="user", scp="gateway.invoke", xms_act_fct="11")
    return jwt.encode(claims, state["key"], algorithm="RS256", headers={"kid": "fixture-key"})


def proxy() -> str:
    return f"http://127.0.0.1:{state['ports']['proxy']}"


def headers(bearer: str | None) -> dict:
    return {"Authorization": f"Bearer {bearer}"} if bearer else {}


def seen() -> list[dict]:
    path = state["root"] / "seen.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class McpSession:
    def __init__(self, bearer: str, server: str = "curriculum") -> None:
        self.bearer, self.server = bearer, server

    async def __aenter__(self) -> ClientSession:
        self.http = httpx2.AsyncClient(headers=headers(self.bearer), follow_redirects=False, timeout=30)
        self.transport = streamable_http_client(f"{proxy()}/mcp/{self.server}", http_client=self.http)
        streams = await self.transport.__aenter__()
        self.session = ClientSession(streams[0], streams[1])
        await self.session.__aenter__()
        await self.session.initialize()
        return self.session

    async def __aexit__(self, *exc) -> None:
        await self.session.__aexit__(*exc)
        await self.transport.__aexit__(*exc)
        await self.http.aclose()


def message(payload: dict, method: str = "SendMessage") -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": {"message": {
        "messageId": "m1", "role": "ROLE_USER", "parts": [{"text": json.dumps(payload)}]}}}


A2A = {"A2A-Version": "1.0"}


def a2a_reply(response: httpx.Response) -> dict:
    return json.loads(response.json()["result"]["message"]["parts"][0]["text"])


def log_text() -> str:
    return (state["root"] / "fixture.log").read_text(errors="replace")


class McpThroughGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_client_reads_the_curriculum_through_the_gateway(self) -> None:
        async with McpSession(token(HOSTED)) as session:
            names = sorted(tool.name for tool in (await session.list_tools()).tools)
            self.assertEqual(names, [
                "curriculum-get_section", "curriculum-get_topic", "curriculum-list_learning_modes",
                "curriculum-list_topics"])
            topics = await session.call_tool("curriculum-list_topics", {})
            self.assertFalse(topics.is_error)
            self.assertIn("fractions", json.dumps(topics.model_dump(mode="json")))
            section = await session.call_tool(
                "curriculum-get_section", {"topic_id": "fractions", "section_id": "fractions-basics", "depth": "intro"})
            self.assertFalse(section.is_error)
            self.assertIn("frac.simplify", json.dumps(section.model_dump(mode="json")))
            bad = await session.call_tool("curriculum-get_section", {"topic_id": "geometry", "section_id": "x"})
            self.assertTrue(bad.is_error)

    async def test_per_tool_grants_are_enforced_by_the_pinned_runtime(self) -> None:
        async with McpSession(token(LIMITED)) as session:
            names = [tool.name for tool in (await session.list_tools()).tools]
            self.assertEqual(names, ["curriculum-list_topics"])
            denied = await session.call_tool("curriculum-get_topic", {"topic_id": "fractions"})
            self.assertTrue(denied.is_error)
            self.assertNotIn("frac.equivalent", json.dumps(denied.model_dump(mode="json")))

    def test_denials_use_the_right_status_and_never_reach_the_backend(self) -> None:
        before = len(seen())
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        cases = [
            ("no token", None, "/mcp/curriculum", 401),
            ("garbage token", "abc.def.ghi", "/mcp/curriculum", 401),
            ("master key", "sk-fixture-master-key-" + "a" * 40, "/mcp/curriculum", 401),
            ("unapproved principal", token(UNAPPROVED), "/mcp/curriculum", 403),
            ("approved principal without grants", token(NOGRANT), "/mcp/curriculum", 403),
            ("quiz principal is inference-only", token(QUIZ), "/mcp/curriculum", 403),
            ("server not granted", token(HOSTED), "/mcp/admin_tools", 403),
            ("delegated user via agent", token(HOSTED, delegated=True), "/mcp/curriculum", 403),
            ("bad path", token(HOSTED), "/mcp/Curriculum", 403),
            ("traversal", token(HOSTED), "/mcp/../key/generate", 403),
        ]
        with httpx.Client(timeout=30) as client:
            for label, bearer, path, expected in cases:
                with self.subTest(label):
                    response = client.post(proxy() + path, headers={
                        **headers(bearer), "content-type": "application/json",
                        "accept": "application/json, text/event-stream"}, json=init)
                    self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(len(seen()), before)

    def test_unsupported_methods_and_routes_are_closed(self) -> None:
        good = token(HOSTED)
        with httpx.Client(timeout=30) as client:
            h = {**headers(good), "content-type": "application/json", "accept": "application/json, text/event-stream"}
            for method in ("prompts/list", "resources/list", "sampling/createMessage", "logging/setLevel"):
                with self.subTest(method):
                    response = client.post(f"{proxy()}/mcp/curriculum", headers=h,
                                           json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
                    self.assertEqual(response.status_code, 403, response.text)
            for label, request in (
                ("batch", client.build_request("POST", f"{proxy()}/mcp/curriculum", headers=h, json=[{"jsonrpc": "2.0"}])),
                ("management", client.build_request("POST", f"{proxy()}/key/generate", headers=h, json={})),
                ("rest tools", client.build_request("GET", f"{proxy()}/mcp-rest/tools/list", headers=h)),
                ("query string", client.build_request("POST", f"{proxy()}/mcp/curriculum?x=1", headers=h, json={})),
                ("get stream", client.build_request("GET", f"{proxy()}/mcp/curriculum", headers=h)),
            ):
                with self.subTest(label):
                    self.assertIn(client.send(request).status_code, (400, 403))


class QuizThroughGatewayTests(unittest.IsolatedAsyncioTestCase):
    def post(self, bearer, body, agent="quiz_master", extra=A2A):
        with httpx.Client(timeout=60) as client:
            return client.post(f"{proxy()}/a2a/{agent}", json=body, headers={**headers(bearer), **(extra or {})})

    def create(self, **overrides):
        request = {"action": "create", "kind": "section", "topic_id": "fractions",
                   "section_id": "fractions-basics", "level": "working", "count": 4, **overrides}
        response = self.post(token(HOSTED), message(request))
        self.assertEqual(response.status_code, 200, response.text)
        return a2a_reply(response)

    def test_create_and_grade_round_trip_with_the_model_reached_through_the_gateway(self) -> None:
        quiz = self.create()
        self.assertTrue(quiz["model_assisted"], "the quiz agent should reach the model through the gateway")
        self.assertEqual(len(quiz["questions"]), 4)
        self.assertTrue(all(q["prompt"].startswith("Quick one! ") for q in quiz["questions"]))
        # Nothing answer-like leaves the quiz service.
        self.assertFalse({"answer", "expected", "answers"} & set(json.dumps(quiz).replace('"', " ").split()))
        wrong = {q["id"]: "zzz" for q in quiz["questions"]}
        graded = a2a_reply(self.post(token(HOSTED), message(
            {"action": "grade", "quiz_id": quiz["quiz_id"], "answers": wrong})))
        self.assertEqual(graded["score"], 0)
        self.assertFalse(graded["passed"])
        self.assertTrue(graded["model_assisted"])
        self.assertTrue(all(r["feedback"] for r in graded["results"]))
        expected = {r["id"]: r["expected"] for r in graded["results"]}
        regraded = a2a_reply(self.post(token(HOSTED), message(
            {"action": "grade", "quiz_id": quiz["quiz_id"], "answers": expected})))
        self.assertEqual((regraded["score"], regraded["max"], regraded["passed"]), (4, 4, True))
        self.assertEqual(regraded["weak_objectives"], [])

    def test_the_quiz_agent_called_the_model_as_its_own_principal(self) -> None:
        self.create(count=3)
        provider = [json.loads(line) for line in (state["root"] / "provider.jsonl").read_text().splitlines()]
        self.assertTrue(provider)
        # The agent asks for the alias; the gateway maps it to the provider's own model name.
        self.assertEqual({entry["model"] for entry in provider}, {"local-test"})
        self.assertFalse({"user", "metadata", "temperature"} & {f for entry in provider for f in entry["fields"]})
        log = log_text()
        self.assertRegex(log, rf"decision=allow reason=ok kind=application oid={QUIZ[0]} azp={QUIZ[1]} route=inference")
        self.assertNotIn("eyJ", log)

    def test_a_tampered_quiz_id_is_refused_without_a_stack_trace(self) -> None:
        quiz = self.create(count=3)
        tampered = quiz["quiz_id"][:-2] + ("AA" if not quiz["quiz_id"].endswith("AA") else "BB")
        reply = a2a_reply(self.post(token(HOSTED), message(
            {"action": "grade", "quiz_id": tampered, "answers": {"q1": "1"}})))
        self.assertEqual(reply["error"]["code"], "invalid_quiz_id")

    def test_a2a_1_0_is_enforced_at_the_gateway(self) -> None:
        good = message({"action": "create", "kind": "diagnostic", "topic_id": "primes"})
        before = len(seen())
        for label, body, extra in (
            ("legacy method", message({"action": "create", "kind": "diagnostic", "topic_id": "primes"}, "message/send"), A2A),
            ("no version header", good, {}),
            ("0.3 version header", good, {"A2A-Version": "0.3"}),
            ("streaming method", message({"action": "create"}, "SendStreamingMessage"), A2A),
            ("task method", message({"action": "create"}, "GetTask"), A2A),
        ):
            with self.subTest(label):
                self.assertEqual(self.post(token(HOSTED), body, extra=extra).status_code, 403)
        self.assertEqual(len(seen()), before)
        self.assertEqual(self.post(token(HOSTED), good).status_code, 200)

    def test_a2a_denials(self) -> None:
        body = message({"action": "create", "kind": "diagnostic", "topic_id": "primes"})
        for label, bearer, agent, expected in (
            ("no token", None, "quiz_master", 401),
            ("unapproved", token(UNAPPROVED), "quiz_master", 403),
            ("approved without agent grant", token(LIMITED), "quiz_master", 403),
            ("quiz principal is inference-only", token(QUIZ), "quiz_master", 403),
            ("agent not granted", token(HOSTED), "finance_agent", 403),
            ("delegated user", token(HOSTED, delegated=True), "quiz_master", 403),
        ):
            with self.subTest(label):
                self.assertEqual(self.post(bearer, body, agent).status_code, expected)

    def test_oversized_and_malformed_requests(self) -> None:
        big = message({"action": "create", "kind": "diagnostic", "topic_id": "primes", "pad": "x" * 70_000})
        self.assertEqual(self.post(token(HOSTED), big).status_code, 413)
        junk = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": "text"}}
        self.assertEqual(self.post(token(HOSTED), junk).status_code, 403)

    def test_agent_card_is_1_0_only_and_requires_a_grant(self) -> None:
        url = f"{proxy()}/a2a/quiz_master/.well-known/agent-card.json"
        with httpx.Client(timeout=30) as client:
            self.assertEqual(client.get(url).status_code, 401)
            self.assertEqual(client.get(url, headers=headers(token(LIMITED))).status_code, 403)
            card = client.get(url, headers=headers(token(HOSTED)))
            legacy = client.get(url.replace("agent-card", "agent"), headers=headers(token(HOSTED)))
        self.assertEqual(card.status_code, 200, card.text)
        self.assertEqual(card.json()["name"], "quiz_master")
        self.assertEqual(legacy.status_code, 403)

    def test_the_callers_credential_is_never_forwarded_to_backends(self) -> None:
        self.create(count=3)
        with httpx.Client(timeout=30) as client:
            client.post(f"{proxy()}/a2a/quiz_master", json=message(
                {"action": "create", "kind": "diagnostic", "topic_id": "primes"}),
                headers={**headers(token(HOSTED)), **A2A, "x-api-key": "caller-secret", "baggage": "caller=secret"})
        names = {name for entry in seen() if entry["backend"] == "a2a" for name in entry["headers"]}
        self.assertTrue(names)
        self.assertFalse(names & {"authorization", "x-api-key", "baggage", "x-client-litellm-authorization"}, names)

    def test_health_probes_stay_anonymous_and_nothing_else_does(self) -> None:
        with httpx.Client(timeout=30) as client:
            for path in ("/health/liveliness", "/health/readiness"):
                self.assertEqual(client.get(proxy() + path).status_code, 200)
            for path in ("/mcp/curriculum", "/a2a/quiz_master", "/v1/agents", "/health", "/"):
                self.assertIn(client.get(proxy() + path).status_code, (401, 403), path)


class AuditLogTests(unittest.TestCase):
    def test_decisions_are_logged_with_identifiers_and_never_secrets(self) -> None:
        with httpx.Client(timeout=30) as client:
            client.post(f"{proxy()}/mcp/curriculum", headers={
                **headers(token(UNAPPROVED)), "accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            client.post(f"{proxy()}/a2a/quiz_master", headers={**headers(token(HOSTED)), **A2A},
                        json=message({}, "message/send"))
            client.post(f"{proxy()}/key/generate", headers=headers(token(HOSTED)), json={})
        log = log_text()
        self.assertRegex(log, rf"decision=deny reason=principal kind=application oid={UNAPPROVED[0]} azp={UNAPPROVED[1]}")
        self.assertRegex(log, r"decision=deny reason=body route=a2a resource=quiz_master rpc=message/send")
        self.assertRegex(log, r"decision=deny reason=route method=POST path=/key/generate")
        for secret in ("eyJ", "sk-fixture-master-key", "Bearer "):
            self.assertNotIn(secret, log)


if __name__ == "__main__":
    unittest.main()
