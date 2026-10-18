"""Curriculum catalogue MCP server for an AI maths tutor: read-only, stateless, no model calls, no network egress."""

import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from catalogue import DEPTHS, ID_RE, load_curriculum

# Fail fast: a malformed data file raises CurriculumError here, before the server accepts traffic.
CATALOGUE = load_curriculum()

mcp = MCPServer("curriculum-catalogue")

ID_HINT = "lowercase letters, digits and hyphens, starting with a letter, max 41 characters"


def _result(payload: dict[str, Any]) -> CallToolResult:
    # Compact JSON text (what the model reads) plus the same payload as structured content.
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)


def _valid(values: Any) -> str:
    return ", ".join(values)


def _check_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ToolError(f"{name} must be {ID_HINT}.")
    return value


def _topic(topic_id: str) -> dict[str, Any]:
    _check_id(topic_id, "topic_id")
    topic = CATALOGUE.topics.get(topic_id)
    if topic is None:
        raise ToolError(f"Unknown topic_id '{topic_id}'. Valid topic ids: {_valid(CATALOGUE.topics)}.")
    return topic


def _section_summary(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": section["id"],
        "title": section["title"],
        "summary": section["summary"],
        "prerequisites": section["prerequisites"],
        "estimated_minutes": section["estimated_minutes"],
        "objectives": section["objectives"],
    }


@mcp.tool()
def list_topics() -> CallToolResult:
    """List all curriculum topics (id, title, one-sentence summary, number of sections)."""
    return _result({
        "topics": [
            {"id": t["id"], "title": t["title"], "summary": t["summary"], "section_count": len(t["sections"])}
            for t in CATALOGUE.topics.values()
        ]
    })


@mcp.tool()
def get_topic(topic_id: str) -> CallToolResult:
    """Get the map of one topic: its sections in teaching order with objectives, but no lesson content.

    topic_id: a topic id from list_topics, e.g. "fractions".
    """
    topic = _topic(topic_id)
    return _result({
        "id": topic["id"],
        "title": topic["title"],
        "summary": topic["summary"],
        "prerequisites": topic["prerequisites"],
        "sections": [_section_summary(s) for s in topic["sections"]],
    })


@mcp.tool()
def get_section(topic_id: str, section_id: str, depth: str = "working") -> CallToolResult:
    """Get one section's teaching content at a single depth.

    topic_id: topic id from list_topics. section_id: section id from get_topic.
    depth: "intro" (gentle, small numbers), "working" (standard method, default) or "deep" (why it works, trickier cases).
    Returns explanation, key points, a worked example, common mistakes and check questions for that depth only.
    """
    topic = _topic(topic_id)
    _check_id(section_id, "section_id")
    sections = topic["sections"]
    index = next((i for i, s in enumerate(sections) if s["id"] == section_id), None)
    if index is None:
        raise ToolError(
            f"Unknown section_id '{section_id}' in topic '{topic_id}'. Valid section ids: {_valid(s['id'] for s in sections)}."
        )
    if not isinstance(depth, str) or depth not in DEPTHS:
        raise ToolError(f"depth must be one of: {_valid(DEPTHS)}.")
    section = sections[index]
    return _result({
        "topic_id": topic_id,
        "section_id": section_id,
        "title": section["title"],
        "depth": depth,
        "objectives": section["objectives"],
        "prerequisites": section["prerequisites"],
        "estimated_minutes": section["estimated_minutes"],
        "content": section["content"][depth],
        "next_section_id": sections[index + 1]["id"] if index + 1 < len(sections) else None,
        "available_depths": list(DEPTHS),
    })


@mcp.tool()
def list_learning_modes() -> CallToolResult:
    """List the learning modes (guided_path, map_then_zoom, mastery_gate) with tutor behaviour and quiz policy."""
    return _result({"modes": CATALOGUE.modes})


async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


def create_app():
    # host="0.0.0.0" disables the localhost-only Host-header check so the app is reachable via ACA/LiteLLM.
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")
    app.add_route("/healthz", healthz, methods=["GET"])
    return app


app = create_app()
