import contextlib
import json
import unittest

import httpx2 as httpx
from mcp.client import Client

from server import CATALOGUE, create_app, mcp

ACCEPT = {"Accept": "application/json, text/event-stream"}
TOOL_NAMES = {"list_topics", "get_topic", "get_section", "list_learning_modes"}
DEPTHS = ["intro", "working", "deep"]
TOPIC_IDS = {"fractions", "primes", "linear-equations"}


class ToolTests(unittest.IsolatedAsyncioTestCase):
    # anyio cancel scopes must be entered and exited in one task, so each call opens its own in-memory client.
    async def call(self, name, **args):
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    async def ok(self, name, **args):
        result = await self.call(name, **args)
        self.assertFalse(result.is_error, result.content)
        # Text content is compact JSON equal to the structured content.
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertNotIn("\n", result.content[0].text)
        return result.structured_content

    async def err(self, name, **args):
        result = await self.call(name, **args)
        self.assertTrue(result.is_error)
        return result.content[0].text

    async def test_lists_exactly_the_four_tools(self):
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
        self.assertEqual({t.name for t in tools}, TOOL_NAMES)
        self.assertEqual(len(tools), 4)
        by_name = {t.name: t for t in tools}
        self.assertEqual(set(by_name["get_section"].input_schema["properties"]), {"topic_id", "section_id", "depth"})
        self.assertEqual(by_name["get_section"].input_schema["required"], ["topic_id", "section_id"])
        self.assertEqual(by_name["get_topic"].input_schema["required"], ["topic_id"])
        self.assertFalse(by_name["list_topics"].input_schema.get("properties"))

    async def test_list_topics(self):
        data = await self.ok("list_topics")
        self.assertEqual({t["id"] for t in data["topics"]}, TOPIC_IDS)
        for topic in data["topics"]:
            self.assertEqual(set(topic), {"id", "title", "summary", "section_count"})
            self.assertEqual(topic["section_count"], 3)
        self.assertEqual({t["title"] for t in data["topics"]}, {"Fractions", "Prime numbers", "Linear equations"})

    async def test_get_topic_is_a_map_without_content(self):
        data = await self.ok("get_topic", topic_id="fractions")
        self.assertEqual(set(data), {"id", "title", "summary", "prerequisites", "sections"})
        self.assertEqual([s["id"] for s in data["sections"]], ["fractions-basics", "fractions-add-subtract", "fractions-multiply-divide"])
        for section in data["sections"]:
            self.assertEqual(
                set(section), {"id", "title", "summary", "prerequisites", "estimated_minutes", "objectives"})
            self.assertTrue(all(set(o) == {"id", "statement"} for o in section["objectives"]))
        self.assertEqual(data["sections"][1]["prerequisites"], ["fractions-basics"])
        self.assertEqual([o["id"] for o in data["sections"][0]["objectives"]], ["frac.equivalent", "frac.simplify"])

    async def test_get_section_returns_only_requested_depth(self):
        for depth in DEPTHS:
            with self.subTest(depth=depth):
                data = await self.ok("get_section", topic_id="primes", section_id="primes-factorization", depth=depth)
                self.assertEqual(
                    set(data),
                    {"topic_id", "section_id", "title", "depth", "objectives", "prerequisites", "estimated_minutes",
                     "content", "next_section_id", "available_depths"})
                self.assertEqual(data["depth"], depth)
                self.assertEqual(data["available_depths"], DEPTHS)
                self.assertEqual(data["content"], CATALOGUE.topics["primes"]["sections"][1]["content"][depth])
                self.assertEqual(
                    set(data["content"]),
                    {"explanation", "key_points", "worked_example", "common_mistakes", "check_questions"})
                self.assertEqual(data["next_section_id"], "primes-gcd-lcm")
                self.assertEqual(data["prerequisites"], ["primes-basics"])

    async def test_get_section_default_depth_is_working_and_last_section_has_no_next(self):
        data = await self.ok("get_section", topic_id="linear-equations", section_id="eq-both-sides")
        self.assertEqual(data["depth"], "working")
        self.assertIsNone(data["next_section_id"])
        self.assertIn("5x + 3 = 2x + 15", data["content"]["worked_example"]["problem"])

    async def test_every_section_and_depth_is_retrievable_and_small(self):
        for topic_id, topic in CATALOGUE.topics.items():
            for section in topic["sections"]:
                for depth in DEPTHS:
                    result = await self.call("get_section", topic_id=topic_id, section_id=section["id"], depth=depth)
                    self.assertFalse(result.is_error)
                    self.assertLess(len(result.content[0].text), 3500, (section["id"], depth))

    async def test_list_learning_modes(self):
        data = await self.ok("list_learning_modes")
        modes = {m["id"]: m for m in data["modes"]}
        self.assertEqual(set(modes), {"guided_path", "map_then_zoom", "mastery_gate"})
        self.assertEqual(modes["mastery_gate"]["quiz_policy"]["kind"], "mastery")
        for mode in modes.values():
            self.assertEqual(
                set(mode), {"id", "title", "description", "tutor_behaviour", "quiz_policy", "default_depth"})

    async def test_unknown_values_list_valid_options(self):
        message = await self.err("get_topic", topic_id="geometry")
        self.assertIn("Unknown topic_id 'geometry'", message)
        for topic_id in TOPIC_IDS:
            self.assertIn(topic_id, message)
        message = await self.err("get_section", topic_id="fractions", section_id="nope")
        self.assertIn("fractions-basics", message)
        self.assertIn("fractions-multiply-divide", message)
        message = await self.err("get_section", topic_id="fractions", section_id="primes-basics")
        self.assertIn("Unknown section_id", message)
        message = await self.err("get_section", topic_id="fractions", section_id="fractions-basics", depth="expert")
        self.assertIn("intro, working, deep", message)
        self.assertNotIn("expert", message)

    async def test_input_validation_rejects_bad_ids(self):
        for bad in ["Fractions", "1fractions", "frac tions", "../etc/passwd", "", "a" * 42, "frac;drop", "fractions\n"]:
            with self.subTest(topic_id=bad[:20]):
                message = await self.err("get_topic", topic_id=bad)
                self.assertIn("topic_id must be", message)
        message = await self.err("get_section", topic_id="fractions", section_id="Bad Section")
        self.assertIn("section_id must be", message)

    async def test_oversized_and_wrong_type_inputs_are_rejected_without_echo(self):
        huge = "a" * 200_000
        for args in [{"topic_id": huge}, {"topic_id": "fractions", "section_id": huge},
                     {"topic_id": "fractions", "section_id": "fractions-basics", "depth": huge}]:
            with self.subTest(keys=sorted(args)):
                name = "get_topic" if len(args) == 1 else "get_section"
                message = await self.err(name, **args)
                self.assertLess(len(message), 400)
                self.assertNotIn("aaaaaaaaaa", message)
        self.assertTrue((await self.call("get_topic", topic_id=7)).is_error)
        self.assertTrue((await self.call("get_topic")).is_error)
        self.assertTrue((await self.call("get_section", topic_id="fractions", section_id="fractions-basics", depth=3)).is_error)


@contextlib.asynccontextmanager
async def http_client():
    # A non-localhost Host header proves the DNS-rebinding check is off (needed behind ACA / LiteLLM).
    app = create_app()  # a session manager can only be started once per app instance
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo-mcp.internal") as http:
            yield http


def rpc(method, params=None, id=1):
    body = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return body


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthz(self):
        async with http_client() as http:
            response = await http.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")

    async def test_initialize_on_mcp_path_with_non_localhost_host(self):
        params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}
        async with http_client() as http:
            response = await http.post("/mcp", headers={**ACCEPT, "Host": "gateway.example.internal"}, json=rpc("initialize", params))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["serverInfo"]["name"], "curriculum-catalogue")

    async def test_tools_list_without_session(self):
        async with http_client() as http:
            response = await http.post("/mcp", headers=ACCEPT, json=rpc("tools/list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual({t["name"] for t in response.json()["result"]["tools"]}, TOOL_NAMES)

    async def test_tools_call_without_session(self):
        body = rpc("tools/call", {"name": "get_section", "arguments": {
            "topic_id": "fractions", "section_id": "fractions-basics", "depth": "intro"}})
        async with http_client() as http:
            response = await http.post("/mcp", headers=ACCEPT, json=body)
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["content"]["worked_example"]["answer"], "2/4")
        self.assertEqual(result["structuredContent"], payload)

    async def test_tool_error_over_http(self):
        body = rpc("tools/call", {"name": "get_topic", "arguments": {"topic_id": "nope"}})
        async with http_client() as http:
            response = await http.post("/mcp", headers=ACCEPT, json=body)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Valid topic ids", result["content"][0]["text"])

    async def test_other_paths_are_not_mcp(self):
        async with http_client() as http:
            self.assertEqual((await http.post("/", json={})).status_code, 404)
            self.assertEqual((await http.get("/mcp/extra")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
