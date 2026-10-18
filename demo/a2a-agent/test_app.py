import json
import logging
import unittest
from unittest import mock

import httpx

from app import MAX_BODY_BYTES, create_app
from quiz_id import QuizIdCodec

KEY = "unit-test-signing-key-0123456789-abcdef"
HEADERS = {"A2A-Version": "1.0"}


def rpc(text=None, method="SendMessage", id_=1, parts=None):
    parts = parts if parts is not None else [{"text": text}]
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": parts}}}


class A2ATests(unittest.IsolatedAsyncioTestCase):
    def client(self, public_url="http://quiz.internal/", headers=HEADERS, **options):
        app = create_app(public_url, signing_key=KEY, **options)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://quiz.internal", headers=headers)

    async def send(self, http, payload):
        response = await http.post("/", json=rpc(json.dumps(payload)))
        self.assertEqual(response.status_code, 200)
        message = response.json()["result"]["message"]
        self.assertEqual(len(message["parts"]), 1)
        return json.loads(message["parts"][0]["text"])

    async def test_healthz(self):
        async with self.client() as http:
            response = await http.get("/healthz")
        self.assertEqual((response.status_code, response.text), (200, "ok"))

    async def test_agent_card_is_native_a2a_1_0_on_the_well_known_path_only(self):
        async with self.client("https://quiz.example/", headers={}) as http:
            response = await http.get("/.well-known/agent-card.json")
            legacy = [await http.get(path) for path in ("/.well-known/agent.json", "/.well-known/agent-card", "/agent.json")]
        self.assertEqual(response.status_code, 200)
        card = response.json()
        self.assertEqual(card["name"], "quiz_master")
        self.assertEqual(
            card["supportedInterfaces"], [{"url": "https://quiz.example/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        )
        self.assertEqual({s["id"] for s in card["skills"]}, {"create-quiz", "grade-quiz"})
        self.assertEqual(card["defaultInputModes"], ["text/plain"])
        self.assertEqual(card["defaultOutputModes"], ["text/plain"])
        self.assertIs(card["capabilities"]["streaming"], False)
        for legacy_field in ("url", "protocolVersion", "preferredTransport", "additionalInterfaces"):
            self.assertNotIn(legacy_field, card)
        self.assertEqual([r.status_code for r in legacy], [404, 404, 404])

    async def test_card_url_defaults_to_localhost_and_follows_public_url_env(self):
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("PUBLIC_URL", None)
            async with self.client(public_url=None) as http:
                card = (await http.get("/.well-known/agent-card.json")).json()
            self.assertEqual(card["supportedInterfaces"][0]["url"], "http://localhost:8080/")
            env["PUBLIC_URL"] = "https://env.example/"
            async with self.client(public_url=None) as http:
                card = (await http.get("/.well-known/agent-card.json")).json()
            self.assertEqual(card["supportedInterfaces"][0]["url"], "https://env.example/")

    async def test_send_message_returns_a_direct_message_with_one_text_part(self):
        async with self.client() as http:
            response = await http.post("/", json=rpc(json.dumps({"action": "create", "kind": "diagnostic", "topic_id": "fractions"}), id_="req-9"))
        body = response.json()
        self.assertEqual((body["jsonrpc"], body["id"]), ("2.0", "req-9"))
        self.assertEqual(set(body["result"]), {"message"})
        message = body["result"]["message"]
        self.assertEqual(message["role"], "ROLE_AGENT")
        self.assertTrue(message["messageId"])
        self.assertEqual(len(message["parts"]), 1)
        self.assertEqual(set(message["parts"][0]), {"text"})
        reply = json.loads(message["parts"][0]["text"])
        self.assertEqual(len(reply["questions"]), 5)

    async def test_create_and_grade_round_trip(self):
        async with self.client() as http:
            quiz = await self.send(http, {"action": "create", "kind": "mastery", "topic_id": "linear-equations", "section_id": "eq-two-step", "level": "intro", "count": 4})
            spec = QuizIdCodec(KEY).decode(quiz["quiz_id"])
            from generators import build_quiz

            answers = {}
            for q in build_quiz(spec.kind, spec.topic, spec.section, spec.level, spec.count, spec.seed):
                answers[q.id] = q.display.split(" = ")[-1]
            graded = await self.send(http, {"action": "grade", "quiz_id": quiz["quiz_id"], "answers": answers})
            self.assertEqual((graded["score"], graded["max"], graded["passed"], graded["pass_threshold"]), (4, 4, True, 0.8))
            wrong = await self.send(http, {"action": "grade", "quiz_id": quiz["quiz_id"], "answers": {**answers, "q1": "banana"}})
            self.assertEqual((wrong["score"], wrong["passed"]), (3, False))
            self.assertEqual(wrong["results"][0]["expected"], build_quiz(spec.kind, spec.topic, spec.section, spec.level, spec.count, spec.seed)[0].display)

    async def test_application_errors_are_normal_messages(self):
        async with self.client() as http:
            for text in ("hello", "[]", json.dumps({"action": "create", "kind": "section", "topic_id": "nope", "section_id": "x"})):
                response = await http.post("/", json=rpc(text))
                body = response.json()
                self.assertNotIn("error", body)
                reply = json.loads(body["result"]["message"]["parts"][0]["text"])
                self.assertIn(reply["error"]["code"], {"invalid_request", "unknown_topic"})

    async def test_wrong_part_types_and_counts_are_reported_as_invalid_requests(self):
        part_sets = [
            [{"data": {"action": "create"}}],
            [{"url": "https://example.com/x"}],
            [{"raw": "aGVsbG8="}],
            [{"text": "{}"}, {"text": "{}"}],
            [],
        ]
        async with self.client() as http:
            for parts in part_sets:
                with self.subTest(parts=parts):
                    response = await http.post("/", json=rpc(parts=parts))
                    body = response.json()
                    if "result" in body:
                        reply = json.loads(body["result"]["message"]["parts"][0]["text"])
                        self.assertEqual(reply["error"]["code"], "invalid_request")
                    else:
                        self.assertEqual(body["error"]["code"], -32602)  # protocol-level rejection of an empty message

    async def test_unknown_and_legacy_methods_return_method_not_found(self):
        async with self.client() as http:
            for method in ("message/send", "message/stream", "tasks/get", "GetTask", "SendStreamingMessage", "CancelTask", "ListTasks", "SubscribeToTask", "sendmessage", "Nope", ""):
                for headers in (HEADERS, {}):
                    with self.subTest(method=method, headers=headers):
                        response = await http.post("/", json=rpc("{}", method=method, id_=7), headers=headers)
                        body = response.json()
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(body["error"]["code"], -32601, body)
                        self.assertEqual(body["id"], 7)

    async def test_legacy_0_3_message_shape_cannot_reach_the_agent(self):
        legacy = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {"kind": "message", "messageId": "m", "role": "user", "parts": [{"kind": "text", "text": "{}"}]}}}
        async with self.client() as http:
            for headers in (HEADERS, {}, {"A2A-Version": "0.3"}):
                body = (await http.post("/", json=legacy, headers=headers)).json()
                self.assertEqual(body["error"]["code"], -32601)

    async def test_requests_must_declare_a2a_version_1(self):
        async with self.client(headers={}) as http:
            body = (await http.post("/", json=rpc("{}"))).json()
            self.assertEqual(body["error"]["code"], -32009)
            body = (await http.post("/", json=rpc("{}"), headers={"A2A-Version": "0.3"})).json()
            self.assertEqual(body["error"]["code"], -32009)
            body = (await http.post("/", json=rpc(json.dumps({"action": "bad"})), headers={"A2A-Version": "1.0"})).json()
            self.assertIn("result", body)
            body = (await http.post("/", json=rpc(json.dumps({"action": "bad"})), headers={"A2A-Version": "1.1"})).json()
            self.assertIn("result", body)

    async def test_malformed_json_rpc(self):
        async with self.client() as http:
            response = await http.post("/", content=b"{not json", headers={"content-type": "application/json"})
            self.assertEqual(response.json()["error"]["code"], -32700)
            for payload in ([], [rpc("{}")], "text", 5, {"jsonrpc": "1.0", "id": 1, "method": "SendMessage"}, {"jsonrpc": "2.0", "id": 1}):
                response = await http.post("/", json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertIn(response.json()["error"]["code"], {-32600, -32601, -32602}, payload)

    async def test_oversized_body_gets_413(self):
        async with self.client() as http:
            response = await http.post("/", json=rpc("x" * (MAX_BODY_BYTES + 10)))
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"]["code"], -32600)
            at_limit = await http.post("/", json=rpc("x" * 2000))
            self.assertEqual(at_limit.status_code, 200)

    async def test_oversized_text_below_the_body_cap_is_an_application_error(self):
        async with self.client() as http:
            response = await http.post("/", json=rpc(json.dumps({"action": "grade", "pad": "y" * 20000})))
            reply = json.loads(response.json()["result"]["message"]["parts"][0]["text"])
        self.assertEqual(reply["error"]["code"], "invalid_request")

    async def test_unexpected_failures_do_not_leak_details(self):
        with mock.patch("service.QuizService.handle_text", side_effect=RuntimeError("secret internals")), self.assertLogs("quiz.app", logging.ERROR) as logs:
            async with self.client() as http:
                response = await http.post("/", json=rpc("{}"))
        reply = json.loads(response.json()["result"]["message"]["parts"][0]["text"])
        self.assertEqual(reply["error"]["code"], "internal_error")
        self.assertNotIn("secret internals", response.text)
        self.assertNotIn("secret internals", "\n".join(logs.output))

    async def test_get_on_the_rpc_path_and_unknown_paths(self):
        async with self.client() as http:
            self.assertEqual((await http.get("/")).status_code, 405)
            self.assertEqual((await http.post("/other", json=rpc("{}"))).status_code, 404)


class ModuleTests(unittest.TestCase):
    def test_module_level_app_is_importable_without_a_signing_key(self):
        import app as module

        self.assertTrue(callable(module.app))


if __name__ == "__main__":
    unittest.main()
