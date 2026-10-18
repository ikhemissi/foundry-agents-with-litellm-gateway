"""Test fixture process: real pinned LiteLLM + our boundary + the real curriculum MCP server and quiz agent.

Mirrors the production layout: the gateway files are copied beside the generated config
(like the ACA secret volume at /etc/litellm), and LiteLLM loads the auth hook from that
directory with its own file-based loader. The quiz agent reaches its model through the same
gateway with its own principal, exactly like the deployed app; a mock provider stands in for
the model. No network or Azure access is used.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ["FIXTURE_DIR"])
REPO = Path(os.environ["REPO_ROOT"])
PORTS = json.loads(os.environ["FIXTURE_PORTS"])
sys.path[:0] = [str(ROOT), str(REPO / "demo" / "mcp-server"), str(REPO / "demo" / "a2a-agent"), str(REPO / "tests" / "support")]

import httpx
import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

os.environ.update({
    "QUIZ_SIGNING_KEY": "fixture-quiz-signing-key-" + "k" * 24,
    "CONFIG_FILE_PATH": str(ROOT / "config.yaml"),
    "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_TELEMETRY": "False",
    "LITELLM_MASTER_KEY": "sk-fixture-master-key-" + "a" * 40,
    "PUBLIC_URL": f"http://127.0.0.1:{PORTS['a2a']}/",
})

import app as a2a_demo  # noqa: E402
import gateway_app  # noqa: E402  (copied beside the config, like production)
import server as mcp_demo  # noqa: E402
from model import GatewayModel  # noqa: E402
from serve import configure_audit_log  # noqa: E402

TENANT = os.environ["ENTRA_TENANT_ID"]
AUDIENCE = os.environ["ENTRA_GATEWAY_CLIENT_ID"]
QUIZ_OID, QUIZ_AZP = os.environ["FIXTURE_QUIZ_OID"], os.environ["FIXTURE_QUIZ_AZP"]
PRIVATE_KEY = (ROOT / "key.pem").read_text()


def quiz_token() -> str:
    now = int(time.time())
    return jwt.encode({
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUDIENCE, "tid": TENANT,
        "oid": QUIZ_OID, "azp": QUIZ_AZP, "sub": QUIZ_OID, "ver": "2.0", "idtyp": "app",
        "iat": now - 1, "nbf": now - 1, "exp": now + 600,
    }, PRIVATE_KEY, algorithm="RS256", headers={"kid": "fixture-key"})


class Recorder:
    """Records which header NAMES each backend receives (never values)."""

    def __init__(self, app, name):
        self.app, self.name = app, name

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            with open(ROOT / "seen.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "backend": self.name, "path": scope["path"],
                    "headers": sorted(name.decode().lower() for name, _ in scope["headers"]),
                }) + "\n")
        await self.app(scope, receive, send)


async def provider_completions(request):
    """Mock model provider: deterministic rewrites and feedback in the shapes the quiz agent expects."""
    body = await request.json()
    with open(ROOT / "provider.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "authorization": request.headers.get("authorization", ""), "model": body.get("model"),
            "fields": sorted(body),
        }) + "\n")
    system, user = body["messages"][0]["content"], json.loads(body["messages"][1]["content"])
    if "questions" in user:
        content = {"prompts": {q["id"]: "Quick one! " + q["prompt"] for q in user["questions"]}}
    else:
        content = {"feedback": {item["id"]: "Look at each step again, then retry." for item in user["items"]}}
    return JSONResponse({
        "id": "mock", "object": "chat.completion", "created": 1, "model": "local-test",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


class ToLocalGateway(httpx.AsyncBaseTransport):
    """The real GatewayModel insists on an https gateway URL; route that name to the local proxy."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=PORTS["proxy"])
        headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"host"]
        forwarded = httpx.Request(request.method, url, headers=headers, content=await request.aread())
        return await self.inner.handle_async_request(forwarded)


async def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def token_provider() -> str:
    return quiz_token()


async def main():
    configure_audit_log()
    provider = Starlette(routes=[Route("/chat/completions", provider_completions, methods=["POST"])])
    model = GatewayModel("https://gateway.test/v1", "gpt-5.4-nano", token_provider, transport=ToLocalGateway())
    servers = [
        await serve(provider, PORTS["provider"]),
        await serve(Recorder(mcp_demo.create_app(), "mcp"), PORTS["mcp"]),
        await serve(Recorder(a2a_demo.create_app(model=model), "a2a"), PORTS["a2a"]),
    ]
    from litellm.proxy import proxy_server

    boundary = gateway_app.CredentialBoundary(proxy_server.app, "entra")
    proxy, task = await serve(boundary, PORTS["proxy"])
    servers.append((proxy, task))
    # The hook is loaded by LiteLLM from this directory, so patch signing keys on that instance.
    hook = proxy_server.user_custom_auth
    verifier = hook.__globals__["verifier"]()
    verifier.keys = {"fixture-key": jwt.PyJWK.from_dict(json.loads((ROOT / "jwk.json").read_text()))}
    verifier.refreshed_at = time.monotonic() + 10**6
    (ROOT / "ready").write_text("ready")
    await asyncio.gather(*(t for _, t in servers))


asyncio.run(main())
