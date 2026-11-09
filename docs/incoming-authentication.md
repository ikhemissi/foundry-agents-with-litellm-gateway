# Incoming authentication

Legacy deployments remain `api_key` unless explicitly migrated. Gateway and
hosted authentication modes are separate settings; changing one does not safely
migrate the other. No failed Entra request falls back to a key or app identity.

| Path / mode | Identity the gateway authorizes |
| --- | --- |
| Legacy prompt / hosted `api_key` | Shared gateway key; no authenticated human attribution |
| Prompt OAuth2 client credentials | Dedicated prompt-client application's service principal (`oid`) and application ID (`azp`), **not** the human or prompt agent's Agent ID |
| Hosted `entra` | Genuine hosted Entra Agent ID service principal and agent application ID |
| Hosted `entra_delegated` | Approved human `oid` plus approved agent actor `azp`, `gateway.invoke` scope, and AgentIdentity actor facet |

[entra_auth.py](../litellm/entra_auth.py) uses LiteLLM's documented OSS
[`custom_auth`](https://docs.litellm.ai/docs/proxy/custom_auth) hook, with PyJWT
signature validation. Built-in `enable_jwt_auth` is Enterprise-gated in
`v1.105.1` and is not enabled. Neither `custom_auth_settings.mode: auto` nor
JWT-to-virtual-key mapping is used. No database, Enterprise license, native
budget, or rate-limit enforcement is implied by the returned auth object.
Model-access enforcement is explicit, rather than relying on `models=[...]`.

The policy accepts only RS256, the configured tenant's exact v2 issuer, and
the gateway application **client ID GUID as audience**. The requested scope is
`api://<gateway-client-id>/.default`; it is not the token's `aud`. Signature,
expiry, not-before, issued-at and required identity claims are checked.
Signing keys come only from that tenant's configured Microsoft Entra v2
JWKS endpoint, never token-supplied URLs. Fetches have 2s connect/5s network
timeouts, a 5-minute cache, and a 30s unknown-key refresh cooldown. Key-service
failures fail closed; a newly rotated key can require waiting out that cooldown.

App-only authorization is an exact allowlist of service-principal/application-ID
pairs (`ENTRA_APP_PRINCIPALS`). A subject is not classified as human merely
because it has `oid`: app requests require `idtyp=app`; delegated requests require
an approved human, approved actor, delegated scope and `xms_act_fct` AgentIdentity
facet `11`. Agent subjects/users (`xms_sub_fct` `11`/`13`) cannot impersonate an
approved human. See Microsoft's [Agent ID claims reference](https://learn.microsoft.com/entra/agent-id/agent-token-claims).

In Entra mode the gateway exposes only these routes, each matched exactly (no
query strings or path variants), and they are classified in
[gateway_policy.py](../litellm/gateway_policy.py):

| Route class | Methods and paths | Request rules |
| --- | --- | --- |
| Health | `GET /health/liveliness`, `/health/readiness` | Anonymous, for the ACA probes |
| Inference | `POST /chat/completions`, `/v1/chat/completions`, `/responses`, `/v1/responses` | Alias `gpt-5.4-nano` only; no provider overrides or unrecognized options; 4 MiB cap |
| MCP (opt-in) | `POST /mcp`, `/mcp/<server>` | JSON-RPC `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call` only; 256 KiB cap |
| A2A 1.0 (opt-in) | `POST /a2a/<agent>` with header `A2A-Version: 1.x`; authenticated `GET /a2a/<agent>/.well-known/agent-card.json` | JSON-RPC `SendMessage` with a message only (no 0.3 `message/send`, streaming, tasks or push); 64 KiB cap |

Management, UI, MCP REST/OAuth routes, `/v1/agents`, other health routes and
everything else return 403. A2A is **1.0 only**: legacy 0.3 callers are refused.

Every authorization decision, and every refusal before authentication, writes one
nonsecret line to the container log (`gateway.audit decision=allow|deny reason=...
kind oid azp route resource rpc`): verified identifiers, route class and JSON-RPC
method, never tokens or bodies. Values are sanitized to one token each. Query it in
Log Analytics (`ContainerAppConsoleLogs_CL`, container `litellm`) to see who was
denied and why, or which methods a new client sends. Caller `user`/`metadata` fields on inference are
discarded, not used as identity or trusted telemetry.

The Entra launcher wraps LiteLLM in [gateway_app.py](../litellm/gateway_app.py) to
remove bearer/alternate credential headers and untrusted tracing baggage
**before** LiteLLM sees request headers. The custom-auth hook still performs
authentication and authorization, returning only nonsecret validated identity.
Raw tokens are never returned as `api_key`/`token` or usage metadata.
Spend persistence and message capture are disabled. This is not a multi-tenant
governance service; it supplies no billing or per-user rate-limit guarantees.

## Hosted modes and playground

[main.py](../agents/ai-tutor-hosted-agent/main.py) supports explicit `api_key`,
`entra`, and `entra_delegated` modes. Only `api_key` constructs a Key Vault
client. `entra` obtains a gateway token with `DefaultAzureCredential`, then
checks that its subject/client match the configured genuine Agent ID and agent
facet before sending it. This is an identity-consistency guard, not a replacement
for gateway signature validation.

Credentials are applied with HTTPX request-local auth, not OpenAI's mutable
shared `api_key` provider state. Redirects and automatic inference retries are
disabled; owned async clients close on server shutdown, including cancellation.
The public `ResponsesHostServer.prepare_options(request, options)` signature was
verified against hosting SDK `1.0.0b261015`. The hook removes **only** Responses
`include`, logging a fixed unsupported-projections warning without the values.
Other options are preserved. Both streaming and non-streaming Responses requests
are tested against their actual downstream Chat Completions bodies.

## Delegation is opt-in, not playground OBO

A trusted caller backend must perform sign-in/consent and the Entra Agent ID
OBO exchange externally. It authenticates to Foundry normally and supplies the
**gateway-audience** user-via-agent token separately in
`x-client-litellm-authorization: Bearer <token>`. Never expose an untrusted
forwarding endpoint that accepts arbitrary users' credentials.

The hosted middleware removes this header before hosting-adapter metadata
handling, holds its token in request-local context, and clears/deactivates it
on completion or cancellation. Foreground requests only are supported;
background inference is rejected. Follow-up requests must provide their own
token; there is no identity fallback or stored-token resumption. HTTP telemetry
credential headers are sanitized and message capture is disabled.

The default policy supports both approved app-only clients and approved
delegation. `ENTRA_DELEGATED_ONLY=true` instead denies app-only on **all** gateway
inference routes, including prompt OAuth2; it does not invent a separate URL
prefix. Use a separately configured gateway for a delegated-only interface
alongside an app-only prompt tutor. The real-proxy tests prove app-only denial
with this policy. CLI preauthorization does not bypass actor/human policy.
This repository does not create a sign-in app, execute OBO, grant live consent,
or make the Foundry playground automatically forward a delegated token.
