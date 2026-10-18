"""A2A 1.0 agent "quiz_master": deterministic quizzes and grading, with optional model-polished wording."""

import json
import logging
import os
from contextlib import asynccontextmanager

from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers.default_request_handler_v2 import DefaultRequestHandlerV2
from a2a.server.request_handlers.response_helpers import build_error_response
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import a2a_pb2
from a2a.utils.errors import MethodNotFoundError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from model import QuizModel, model_from_env
from service import QuizService

MAX_BODY_BYTES = 64 * 1024
SUPPORTED_METHOD = "SendMessage"
TEXT = "text/plain"
SIGNING_KEY_ENV = "QUIZ_SIGNING_KEY"

log = logging.getLogger("quiz.app")


def build_card(public_url: str) -> a2a_pb2.AgentCard:
    return a2a_pb2.AgentCard(
        name="quiz_master",
        description=(
            "Creates short maths quizzes (fractions, primes, linear equations) and grades them. Questions and "
            "scoring are deterministic and computed in code; a language model may only polish wording and feedback."
        ),
        version="2.0.0",
        supported_interfaces=[
            a2a_pb2.AgentInterface(url=public_url, protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        capabilities=a2a_pb2.AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=[TEXT],
        default_output_modes=[TEXT],
        skills=[
            a2a_pb2.AgentSkill(
                id="create-quiz",
                name="Create quiz",
                description=(
                    'Send a JSON text part {"action":"create","kind":"diagnostic|section|mastery","topic_id":...,'
                    '"section_id":... (section/mastery),"level":"intro|working|deep","count":3-10}. '
                    "Replies with a signed quiz_id and questions; answers are never included."
                ),
                tags=["maths", "quiz", "fractions", "primes", "linear-equations"],
                examples=['{"action":"create","kind":"section","topic_id":"fractions","section_id":"fractions-basics"}'],
                input_modes=[TEXT],
                output_modes=[TEXT],
            ),
            a2a_pb2.AgentSkill(
                id="grade-quiz",
                name="Grade quiz",
                description=(
                    'Send a JSON text part {"action":"grade","quiz_id":...,"answers":{"q1":"3/4",...}}. '
                    "Replies with score, per-question results, worked solutions for wrong answers and weak objectives."
                ),
                tags=["maths", "quiz", "grading"],
                examples=['{"action":"grade","quiz_id":"<quiz_id from create>","answers":{"q1":"3/4","q2":"x=5"}}'],
                input_modes=[TEXT],
                output_modes=[TEXT],
            ),
        ],
    )


def _error_text(code: str, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message}})


class QuizExecutor(AgentExecutor):
    def __init__(self, service: QuizService) -> None:
        self.service = service

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(new_text_message(await self._reply(context)))

    async def _reply(self, context: RequestContext) -> str:
        parts = list(context.message.parts) if context.message else []
        if len(parts) != 1 or parts[0].WhichOneof("content") != "text":
            return _error_text("invalid_request", "Send exactly one text part containing a JSON object.")
        try:
            return await self.service.handle_text(parts[0].text)
        except Exception as error:  # noqa: BLE001 - never leak internals to the caller
            log.error("unexpected failure: %s", type(error).__name__)
            return _error_text("internal_error", "The quiz agent failed to process the request.")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported: every request completes immediately")


class BodyLimitMiddleware:
    """Rejects request bodies over `max_bytes` with 413 before they reach the app."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[Message] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # client disconnected
                return
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                body = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": f"Request body exceeds {self.max_bytes} bytes"},
                }
                await JSONResponse(body, status_code=413)(scope, receive, send)
                return
            chunks.append(message)
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return chunks.pop(0) if chunks else {"type": "http.disconnect"}

        await self.app(scope, replay, send)


def _only_send_message(endpoint):
    """Answer every JSON-RPC method except SendMessage with -32601 (the SDK also serves task and streaming methods)."""

    async def guarded(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return await endpoint(request)  # let the SDK produce the parse error
        method = body.get("method") if isinstance(body, dict) else None
        if isinstance(method, str) and method != SUPPORTED_METHOD:
            request_id = body.get("id")
            request_id = request_id if isinstance(request_id, str | int) else None
            return JSONResponse(build_error_response(request_id, MethodNotFoundError()))
        return await endpoint(request)

    return guarded


def create_app(
    public_url: str | None = None,
    model: QuizModel | None = None,
    signing_key: str | None = None,
    seed_factory=None,
) -> ASGIApp:
    """Build the ASGI app. Fails fast without a signing key or with an invalid model configuration.

    `model` injects a QuizModel (tests, integration); otherwise QUIZ_MODEL_MODE decides (default: off).
    `seed_factory` is a test hook that makes quiz seeds predictable; it is not reachable from the wire.
    """
    key = signing_key if signing_key is not None else os.environ.get(SIGNING_KEY_ENV, "")
    owned_model = model is None
    quiz_model = model_from_env() if owned_model else model
    service = QuizService(key, quiz_model, **({"seed_factory": seed_factory} if seed_factory else {}))
    card = build_card(public_url or os.environ.get("PUBLIC_URL", "http://localhost:8080/"))

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    handler = DefaultRequestHandlerV2(QuizExecutor(service), InMemoryTaskStore(), card)
    rpc_routes = create_jsonrpc_routes(handler, "/", enable_v0_3_compat=False)
    routes = [
        *create_agent_card_routes(card),
        Route("/healthz", healthz, methods=["GET"]),
        *(Route(route.path, _only_send_message(route.endpoint), methods=["POST"]) for route in rpc_routes),
    ]

    @asynccontextmanager
    async def lifespan(_: Starlette):
        yield
        if owned_model and hasattr(quiz_model, "aclose"):
            await quiz_model.aclose()

    return BodyLimitMiddleware(Starlette(routes=routes, lifespan=lifespan))


class _LazyApp:
    """Module-level ASGI app for `uvicorn app:app`; built on first use so importing never needs the signing key."""

    def __init__(self) -> None:
        self._app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app: ASGIApp = _LazyApp()
