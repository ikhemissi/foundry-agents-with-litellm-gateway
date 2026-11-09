# Monitor, validate and operate

## Shared monitoring

[monitoring.bicep](../infra/modules/monitoring.bicep) creates one workspace-backed
Application Insights resource and its Log Analytics workspace. Local ingestion
authentication is disabled. Both Foundry projects connect to it with **Project
Managed Identity**, and each project identity receives **Monitoring Metrics
Publisher** and **Monitoring Reader** on that component. The App Insights
connection waits for both component-scoped assignments; the reader is the
**project's** identity, not the account identity. Project-managed trace ingestion
is a Foundry preview.

LiteLLM emits OpenTelemetry spans to a loopback-only collector sidecar, which
exports using the gateway managed identity and the same publisher role. Unlike
ACA's managed Application Insights exporter, this supports Entra-only ingestion.
The connection string identifies the component; it does not replace authentication.
Gateway prompt/response capture is disabled. Foundry traces can contain sensitive
GenAI data, which the deployer's explicit reader roles allow them to access.

The hosted tutor emits Agent Framework/OpenTelemetry traces to the same component
using its own identity, with message-content capture disabled. The prompt tutor
continues using Foundry's project-managed tracing.

Foundry accounts/projects, Key Vault, ACA, and the ACA environment send supported
resource logs and metrics to the same workspace. ACA uses Azure Monitor diagnostic
settings instead of workspace shared keys. Resource diagnostics are not Application
Insights traces; managed identities, role assignments, and secrets have no separate
diagnostic stream (vault operations are covered by vault audit logs).

azd exports `APPLICATIONINSIGHTS_NAME`, `APPLICATIONINSIGHTS_RESOURCE_ID`,
`APPLICATIONINSIGHTS_CONNECTION_STRING`, and `AZURE_LOG_ANALYTICS_WORKSPACE_ID`.
Use **Application Insights > Logs** or **Foundry > Agents > Traces** to inspect
telemetry. Resource logs are queried in the shared workspace. Ingestion may take
several minutes and incurs Azure Monitor charges.

## Validate and operate

```bash
# Compile Bicep and run local contract/unit tests (no resource deployment).
node --test tests/*.test.mjs
agents/ai-tutor-hosted-agent/.venv/bin/python -m unittest discover \
  -s agents/ai-tutor-hosted-agent -p 'test_*.py'

# Separate local environment for the pinned real LiteLLM tests.
python3.13 -m venv .venv
.venv/bin/python -m pip install -r tests/requirements.gateway.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# Demo backends (their own pinned requirements; run each in a throwaway environment).
(cd demo/mcp-server && python -m unittest discover)
(cd demo/a2a-agent && python -m unittest discover)
```

Local tests use generated RSA tokens, mocked JWKS and inference services, the
real LiteLLM `1.105.1` HTTP/auth stack (including its file-based hook loader), and
the pinned hosting SDK/OpenAI HTTP transport. They verify denial without
inference/fallback, strict claims/routes/models, health probes, simulated signing
key rotation/outage, legacy behavior, `include` handling, concurrent user
isolation, cancellation, cleanup and token redaction. Prisma's Python package
is a **test dependency** for LiteLLM's error classifier; no database is started.
The MCP/A2A tests run the real pinned LiteLLM, our boundary and the real demo
backends in a separate process (mirroring the `/etc/litellm` layout) and drive them
with the official `mcp` client: per-tool grants, per-agent grants, denied principals,
closed routes and methods, and that no caller credential reaches a backend.

## Live verification (one test environment)

Beyond the local tests, the following were exercised against the live `uksouth`
sample environment. Nothing else here is claimed as Azure-verified.

| Verified live | Result |
| --- | --- |
| Monitoring Reader + Publisher on the App Insights component for both project identities | Present, component-scoped; connections intact |
| Legacy `api_key` gateway and hosted tutor after provisioning | 401 without key, 200 with key; tutor works |
| Hosted Responses with `include` (streaming, non-streaming, empty) | 200 |
| Graph registration (new apps, v2 tokens, `gateway.invoke`, optional claims) | Created; repeat deployment leaves IDs unchanged |
| Entra gateway on the stock image | Healthy; probes 200; all other routes 403; master key 401; no fallback |
| Real prompt-client token claims | `aud`=gateway client-ID GUID, `ver` 2.0, `idtyp` app, expected `azp`/`oid` |
| Policy: valid/streaming inference; other model, `api_base`, `/key/*`, `/v1/models`, wrong-audience token | 200 / 403 / 403 / 403 / 403 / 401 |
| Prompt tutor via ModelGateway OAuth2 client credentials | Works; credential in Key Vault only |
| Explicit rotation, then retiring the old credential | Works; tutor unaffected |
| Hosted tutor in `entra` mode with its genuine Agent ID (`agentIdentity`) | Works with no Key Vault access |
| Revocation script removing the lingering hosted secret-reader assignment | Removed; telemetry publisher kept; tutor still works |
| Telemetry scan after the above | Traces ingested; no JWTs, bearer headers or master key found |
| Demo registry and two internal apps; remote image builds; deploy | Created; apps run the registry images; internal ingress only (outside requests get the environment's 404 page) |
| Curriculum MCP server and `quiz_master` A2A 1.0 agent (internal apps, stock LiteLLM image) behind the gateway; hosted tutor with `GATEWAY_TOOLS=enabled` (agent version 5, then the short-id build) | Tutor listed learning modes and topics, followed the chosen mode, created a quiz through A2A (`A2A-Version: 1.0`, `SendMessage`) and graded it on the next turn with the exact short quiz id: score, per-question explanations and feedback returned |
| Quiz agent's own managed identity (no Azure RBAC) calling the gateway for question rewording and feedback | Allowed for inference only (`oid` = quiz identity); the gateway audit trail shows tutor identity on `mcp`/`a2a` routes and quiz identity on `inference` |
| First-version ~170-character quiz id | Failed live: the tutor model corrupted the id while copying it. Replaced by the 24-character id; the re-test passed |
| Throwaway prompt agent using the gateway's MCP tools through a Foundry toolbox, and the A2A quiz agent as a direct agent tool (spike, resources deleted afterwards) | MCP via toolbox and A2A direct worked end to end under the agents' own identities; A2A via toolbox did not (see the spike findings) |
| Production prompt tutor with `PROMPT_TOOLS=enabled` (agent version 2: toolbox MCP tool plus direct A2A tool), its own agent identity approved on the gateway | Plain answers unchanged; listed topics and learning modes from the catalogue; created and graded a quiz (3/3). Gateway audit: that identity made the `mcp`, `a2a_card` and `SendMessage` calls, only session-close `DELETE`s denied; re-running the hook made no new version; telemetry had no JWTs or bearer headers (the quiz id appears in conversation content) |
| Unauthenticated calls to `/mcp/curriculum`, `/a2a/quiz_master`, the agent card, `/v1/chat/completions` | 401 each; `/v1/models`, `/ui`, `/openapi.json` 403; liveness 200; non-POST `GET /mcp/curriculum` denied by route policy |
| Prompt-client token (approved for inference only) on MCP, A2A, agent card | 403 on all three; inference still 200; master key 401; MCP REST and `/v1/agents` 403 |
| Telemetry scan after the tool calls (466 records, gateway and hosted agent) | No JWTs, bearer headers, master key, prompt secret or forwarding header |

**Not verified:** A2A through a Foundry toolbox with an anonymous card, tool approvals, MCP/A2A with real third-party backends or Entra-protected backends, a user-via-agent token reaching tools (denied by policy; untested live), Azure Functions-hosted backends, delegated (`entra_delegated`) operation (it needs a trusted
sign-in/OBO backend, consent and approved human/actor IDs that do not exist
here), registration *adoption* of pre-existing applications, other tenants,
Foundry portal Insights rendering with the new reader role, and Agent ID
credential refresh over time. Rotation was verified once; token caching inside
Foundry can delay when a retired secret would be noticed.

In the consumer Foundry portal, inspect the connection under **Manage > Resource
details > Admin-connected models**, and select either `ai-tutor-prompt-agent` or
`ai-tutor-hosted-agent` to test the independent tutors. Inference and hosted-agent
compute incur usage charges.

This is a public-network demonstration, not a production landing zone:

- LiteLLM is pinned to `v1.105.1`, with one always-on replica, 1 vCPU, 2 GiB,
  and no Postgres/Redis. There is no persistent spend tracking or high availability.
- Its OpenTelemetry collector sidecar adds 0.25 vCPU and 0.5 GiB per replica.
- Log Analytics retains logs for 30 days. Avoid logging sensitive prompts.
- RBAC propagation may delay initial requests. ACA capacity errors may require
  another supported region or a later retry; model quota does not guarantee ACA capacity.
- ACA checks versionless Key Vault references within 30 minutes. Mounted config
  changes may require restarting the active revision.
- Vault purge protection and Foundry soft deletion can reserve names after cleanup.
  Let deletion finish and use a new environment for clean retries; do not purge automatically.
