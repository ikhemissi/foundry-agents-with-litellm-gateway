"""Authenticated access to the gateway: request-local credentials, MCP tools and A2A agents."""

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from agent_framework import MCPStreamableHTTPTool, tool

log = logging.getLogger(__name__)
AUTH_SCHEME = "Bearer"
MCP_SERVER = "curriculum"
A2A_AGENT = "quiz_master"
A2A_VERSION = "1.0"
MAX_A2A_RESPONSE_BYTES = 65_536
RETRY_SECONDS = 30
ID = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
QUIZ_KINDS = ("diagnostic", "section", "mastery")
DEPTHS = ("intro", "working", "deep")

TOOL_GUIDANCE = (
    "\n\nYou can use these tools whenever they help (if one is unavailable, carry on from your own "
    "knowledge and say so briefly):\n"
    "- curriculum-list_learning_modes, curriculum-list_topics, curriculum-get_topic, "
    "curriculum-get_section: the course catalogue. Ground lessons in it and do not invent topics.\n"
    "- create_quiz and grade_quiz: a quiz service. Never state, guess or hint at a quiz answer yourself. "
    "Show the learner the questions, collect their answers, then call grade_quiz with the question ids "
    "and the learner's exact answers. Copy the quiz_id character for character from the create_quiz result "
    "(never retype or shorten it) and keep it in the conversation until the quiz is graded.\n"
    "Learning flow: at the start ask what the learner wants to learn, how deep to go (intro, working or "
    "deep) and how they like to learn; offer the modes from list_learning_modes and follow the chosen "
    "mode's tutor behaviour. Teach one section at a time using get_section at the chosen depth. At the "
    "end of each section offer a short section quiz. After grading, acknowledge what was right, explain "
    "mistakes using the feedback, and revisit weak objectives before moving on. Never mention tool "
    "credentials, identifiers or internal error details."
)


def origin_of(url: str | httpx.URL) -> tuple[str, str, int]:
    parsed = httpx.URL(str(url))
    return parsed.scheme, parsed.host, parsed.port or (443 if parsed.scheme == "https" else 80)


class RequestCredentialAuth(httpx.Auth):
    """Attach a credential per request, optionally only to one origin."""

    def __init__(self, get_credential: Callable[[], Awaitable[str]], origin: str | None = None) -> None:
        self.get_credential = get_credential
        self.origin = origin_of(origin) if origin else None

    async def async_auth_flow(
        self, request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self.origin is not None and origin_of(request.url) != self.origin:
            raise RuntimeError("The gateway credential is bound to the gateway origin.")
        request.headers["Authorization"] = f"{AUTH_SCHEME} {await self.get_credential()}"
        try:
            yield request
        finally:
            request.headers.pop("Authorization", None)


def gateway_origin(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def a2a_text(payload: Any) -> str:
    """Extract the agent's text from an A2A 1.0 SendMessage result (protobuf JSON)."""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise ValueError("Unexpected A2A response.")
    parts: list[Any] = []
    if isinstance(result.get("message"), dict):
        parts = result["message"].get("parts") or []
    elif isinstance(result.get("task"), dict):
        task = result["task"]
        parts = ((task.get("status") or {}).get("message") or {}).get("parts") or [
            part for artifact in task.get("artifacts") or [] for part in artifact.get("parts") or []
        ]
    texts = [part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    if not texts:
        raise ValueError("The A2A agent returned no text.")
    return "\n".join(texts)


class GatewayTools:
    """MCP tools and an A2A agent reached through the gateway with the agent's own credential."""

    def __init__(
        self,
        base_url: str,
        get_credential: Callable[[], Awaitable[str]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.origin = gateway_origin(base_url)
        self.client = httpx.AsyncClient(
            transport=transport, follow_redirects=False,
            timeout=httpx.Timeout(30, connect=5),
            auth=RequestCredentialAuth(get_credential, self.origin),
        )
        # Prompts are not exposed by the gateway policy, so never request them.
        self.mcp = MCPStreamableHTTPTool(
            name=MCP_SERVER, url=f"{self.origin}/mcp/{MCP_SERVER}", http_client=self.client,
            load_prompts=False, approval_mode="never_require",
        )
        self.create_quiz = tool(
            self._create_quiz, name="create_quiz", approval_mode="never_require",
            description="Create a short quiz (diagnostic for a topic, or section/mastery for one section). "
                        "Returns a quiz_id and the questions, without answers.",
        )
        self.grade_quiz = tool(
            self._grade_quiz, name="grade_quiz", approval_mode="never_require",
            description="Grade a quiz: pass the quiz_id and the learner's exact answers keyed by question id. "
                        "Returns the score, which questions were wrong, and the objectives to revisit.",
        )
        self._lock = asyncio.Lock()
        self._retry_at = 0.0

    @property
    def tools(self) -> list[Any]:
        return [self.mcp, self.create_quiz, self.grade_quiz]

    async def ensure_connected(self) -> None:
        """Connect lazily and tolerate a gateway that is not ready; the tutor still works without tools."""
        if self.mcp.is_connected:
            return
        async with self._lock:
            now = time.monotonic()
            if self.mcp.is_connected or now < self._retry_at:
                return
            try:
                await self.mcp.connect()
            except Exception as error:  # noqa: BLE001 - never log gateway responses or credentials
                self._retry_at = now + RETRY_SECONDS
                log.warning("Gateway MCP tools are unavailable (%s).", type(error).__name__)

    async def _create_quiz(
        self,
        kind: Literal["diagnostic", "section", "mastery"],
        topic_id: str,
        section_id: str | None = None,
        level: Literal["intro", "working", "deep"] = "working",
        count: int | None = None,
    ) -> str:
        """Create a quiz."""
        if kind not in QUIZ_KINDS or level not in DEPTHS or not ID.match(topic_id or ""):
            return json.dumps({"error": {"code": "invalid_request", "message": "Invalid quiz request."}})
        request: dict[str, Any] = {"action": "create", "kind": kind, "topic_id": topic_id, "level": level}
        if section_id is not None:
            if not ID.match(section_id):
                return json.dumps({"error": {"code": "invalid_request", "message": "Invalid section id."}})
            request["section_id"] = section_id
        if count is not None:
            request["count"] = int(count)
        return await self._send(request)

    async def _grade_quiz(self, quiz_id: str, answers: dict[str, str]) -> str:
        """Grade a quiz."""
        if (
            not isinstance(quiz_id, str) or not 0 < len(quiz_id) <= 1024
            or not isinstance(answers, dict) or not 0 < len(answers) <= 10
            or any(not isinstance(k, str) or len(k) > 8 or not isinstance(v, str) or len(v) > 64
                   for k, v in answers.items())
        ):
            return json.dumps({"error": {"code": "invalid_request", "message": "Invalid grading request."}})
        return await self._send({"action": "grade", "quiz_id": quiz_id, "answers": answers})

    async def _send(self, request: dict[str, Any]) -> str:
        message = {
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendMessage",
            "params": {"message": {
                "messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                "parts": [{"text": json.dumps(request, separators=(",", ":"))}],
            }},
        }
        try:
            response = await self.client.post(
                f"{self.origin}/a2a/{A2A_AGENT}", json=message, headers={"A2A-Version": A2A_VERSION},
            )
            response.raise_for_status()
            if len(response.content) > MAX_A2A_RESPONSE_BYTES:
                raise ValueError("The A2A response is too large.")
            return a2a_text(json.loads(response.content))
        except (httpx.HTTPError, ValueError) as error:
            log.warning("The A2A call failed (%s).", type(error).__name__)
            return json.dumps({"error": {"code": "unavailable", "message": "The quiz service is unavailable right now."}})

    async def aclose(self) -> None:
        try:
            await self.mcp.close()
        finally:
            await self.client.aclose()
