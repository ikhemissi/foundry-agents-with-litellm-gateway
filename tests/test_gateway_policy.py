"""Pure policy tests: route classification, body rules and the capabilities parser."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "litellm"))

from entra_auth import parse_capabilities  # noqa: E402
from gateway_policy import (  # noqa: E402
    Route, a2a_version_ok, audit, classify, forwardable_headers, rpc_method, safe, validate_body,
)

OID = "aaaaaaaa-0000-0000-0000-000000000001"
APPS = frozenset({(OID, "bbbbbbbb-0000-0000-0000-000000000001")})


class ClassifyTests(unittest.TestCase):
    def test_permitted_routes(self) -> None:
        for method, path, expected in (
            ("GET", "/health/liveliness", Route("health")),
            ("GET", "/health/readiness", Route("health")),
            ("POST", "/v1/chat/completions", Route("inference")),
            ("POST", "/responses", Route("inference")),
            ("POST", "/mcp", Route("mcp")),
            ("POST", "/mcp/curriculum", Route("mcp", "curriculum")),
            ("POST", "/a2a/quiz_master", Route("a2a", "quiz_master")),
            ("GET", "/a2a/quiz_master/.well-known/agent-card.json", Route("a2a_card", "quiz_master")),
        ):
            with self.subTest(path=path):
                self.assertEqual(classify(method, path), expected)

    def test_everything_else_is_closed(self) -> None:
        for method, path in (
            ("GET", "/health"), ("GET", "/"), ("GET", "/v1/models"), ("GET", "/mcp"), ("DELETE", "/mcp"),
            ("POST", "/mcp/"), ("POST", "/mcp/Math"), ("POST", "/mcp/../key/generate"), ("POST", "/mcp/a/b"),
            ("POST", "/mcp-rest/tools/call"), ("POST", "/a2a/"), ("POST", "/a2a/x/message/send"),
            ("POST", "/v1/agents"), ("POST", "/key/generate"),
            ("POST", "/a2a/quiz_master/.well-known/agent-card.json"), ("GET", "/a2a/p/../q/.well-known/agent.json"),
            ("GET", "/a2a/quiz_master/.well-known/agent.json"), ("POST", "/v1/a2a/quiz_master/message/send"),
            ("GET", "/health/readiness/"), ("PUT", "/responses"), ("POST", "/mcp/" + "a" * 42),
        ):
            with self.subTest(method=method, path=path):
                self.assertIsNone(classify(method, path))


class BodyTests(unittest.TestCase):
    def rpc(self, method, params=None, **extra):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, **extra}
        if params is not None:
            body["params"] = params
        return body

    def test_mcp_methods(self) -> None:
        mcp = Route("mcp", "curriculum")
        for method in ("initialize", "notifications/initialized", "tools/list", "ping"):
            self.assertIsNone(validate_body(mcp, self.rpc(method, {})), method)
        self.assertIsNone(validate_body(mcp, self.rpc("tools/call", {"name": "curriculum-get_section", "arguments": {"topic_id": "fractions"}})))
        for method in ("prompts/list", "prompts/get", "resources/list", "resources/read", "sampling/createMessage",
                       "elicitation/create", "logging/setLevel", "completion/complete", "tasks/get"):
            self.assertIsNotNone(validate_body(mcp, self.rpc(method, {})), method)

    def test_malformed_requests(self) -> None:
        mcp = Route("mcp")
        for body in (
            {"jsonrpc": "1.0", "method": "ping"}, {"method": "ping"}, self.rpc("ping", [], ),
            self.rpc("ping", {}, extra=1), self.rpc("ping", {}, id=[1]),
            self.rpc("tools/call", {"name": 5}), self.rpc("tools/call", {"name": "bad name!"}),
            self.rpc("tools/call", {"name": "ok", "arguments": []}), self.rpc("tools/call", {"name": "a" * 129}),
        ):
            with self.subTest(body=body):
                self.assertIsNotNone(validate_body(mcp, body))

    def test_a2a_accepts_only_a_1_0_send_message_with_a_message(self) -> None:
        a2a = Route("a2a", "quiz_master")
        message = {"message": {"role": "ROLE_USER", "parts": [{"text": "{}"}]}}
        self.assertIsNone(validate_body(a2a, self.rpc("SendMessage", message)))
        self.assertIsNone(validate_body(a2a, self.rpc("SendMessage", {**message, "configuration": {"blocking": True}})))
        for method in ("message/send", "message/stream", "SendStreamingMessage", "GetTask", "CancelTask",
                       "tasks/get", "tasks/cancel", "SubscribeToTask", "GetExtendedAgentCard"):
            self.assertIsNotNone(validate_body(a2a, self.rpc(method, message)), method)
        for params in ({}, {"message": "text"}, {**message, "pushNotificationConfig": {}}, {**message, "tenant": "t"}):
            self.assertIsNotNone(validate_body(a2a, self.rpc("SendMessage", params)), params)

    def test_inference_rules_are_unchanged(self) -> None:
        inference = Route("inference")
        self.assertIsNone(validate_body(inference, {"model": "m", "messages": []}))
        self.assertIsNotNone(validate_body(inference, {"model": "m", "api_base": "http://x"}))
        self.assertIsNotNone(validate_body(inference, {"model": "m", "fallbacks": []}))


class HeaderTests(unittest.TestCase):
    def test_only_expected_headers_are_forwarded_per_route_class(self) -> None:
        raw = [(b"Authorization", b"x"), (b"X-Api-Key", b"x"), (b"Baggage", b"x"), (b"Content-Type", b"a"),
               (b"accept", b"b"), (b"host", b"h"), (b"mcp-session-id", b"s1"), (b"mcp-protocol-version", b"2025-06-18"),
               (b"a2a-version", b"0.3"), (b"x-mcp-servers", b"admin")]
        names = lambda route: {n for n, _ in forwardable_headers(route, raw)}
        self.assertEqual(names(Route("inference")), {b"content-type", b"accept", b"host"})
        self.assertEqual(names(Route("mcp")), {b"content-type", b"accept", b"host", b"mcp-session-id", b"mcp-protocol-version"})
        self.assertEqual(names(Route("a2a", "x")), {b"content-type", b"accept", b"host", b"a2a-version"})

    def test_oversized_or_non_printable_extra_headers_are_dropped(self) -> None:
        for value in (b"x" * 201, b"bad\x00value", "é".encode()):
            self.assertEqual(forwardable_headers(Route("mcp"), [(b"mcp-session-id", value)]), [])


class A2aVersionTests(unittest.TestCase):
    def test_only_a_single_1_x_declaration_is_accepted(self) -> None:
        self.assertTrue(a2a_version_ok([(b"A2A-Version", b"1.0")]))
        self.assertTrue(a2a_version_ok([(b"a2a-version", b"1.2")]))
        for headers in ([], [(b"a2a-version", b"0.3")], [(b"a2a-version", b"2.0")], [(b"a2a-version", b"")],
                        [(b"a2a-version", b"1.0"), (b"a2a-version", b"1.0")], [(b"a2a-version", b"1." + b"0" * 20)]):
            with self.subTest(headers=headers):
                self.assertFalse(a2a_version_ok(headers))


class AuditTests(unittest.TestCase):
    def test_values_are_sanitized_to_one_safe_token(self) -> None:
        self.assertEqual(safe("a b\nc\x00d"), "a?b?c?d")
        self.assertEqual(safe(""), "-")
        self.assertEqual(len(safe("x" * 500)), 64)
        self.assertEqual(rpc_method({"method": "tools/call"}), "tools/call")
        self.assertIsNone(rpc_method({"method": 5}))
        self.assertIsNone(rpc_method([]))

    def test_a_decision_line_carries_identifiers_but_nothing_injected(self) -> None:
        with self.assertLogs("gateway.audit", level="INFO") as logs:
            audit("deny", "principal", oid="aaaa", azp="bbbb", route="mcp", resource="curriculum",
                  rpc="tools/call\ndecision=allow", skipped=None)
        line = logs.records[0].getMessage()
        self.assertEqual(line.count("\n"), 0)
        self.assertTrue(line.startswith("decision=deny reason=principal "))
        self.assertIn("oid=aaaa", line)
        self.assertNotIn("skipped", line)
        self.assertNotIn("decision=allow", line)


class CapabilitiesTests(unittest.TestCase):
    def parse(self, document):
        return parse_capabilities(json.dumps(document), APPS)

    def test_valid_grants(self) -> None:
        parsed = self.parse({OID: {"mcp": {"curriculum": ["is_prime"]}, "agents": ["problem_setter"]}})
        self.assertEqual(parsed[OID].mcp, {"curriculum": frozenset({"is_prime"})})
        self.assertEqual(parsed[OID].agents, frozenset({"problem_setter"}))
        self.assertEqual(self.parse({}), {})

    def test_invalid_grants_fail_closed_at_startup(self) -> None:
        other = "cccccccc-0000-0000-0000-000000000009"
        for document in (
            [], {other: {"agents": ["a"]}}, {"not-a-guid": {}}, {OID: []}, {OID: {"extra": 1}},
            {OID: {"mcp": []}}, {OID: {"mcp": {"Bad": ["t"]}}}, {OID: {"mcp": {"s": []}}},
            {OID: {"mcp": {"s": ["bad tool"]}}}, {OID: {"mcp": {"s": [1]}}}, {OID: {"agents": "a"}},
            {OID: {"agents": ["Bad-Name"]}},
        ):
            with self.subTest(document=document), self.assertRaises((ValueError, TypeError)):
                self.parse(document)


if __name__ == "__main__":
    unittest.main()
