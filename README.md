# LiteLLM on Azure Container Apps with Microsoft Foundry

An azd + Bicep demo of **Microsoft Foundry and LiteLLM integration in both
directions**: Foundry agents consume models through LiteLLM, and LiteLLM calls a
model hosted in Foundry using managed identity.

Two independent AI tutors illustrate the agent-side integration:

- **Prompt agent:** Foundry routes model requests through a ModelGateway connection
  to LiteLLM.
- **Hosted agent:** Python code built with Microsoft Agent Framework runs in
  Foundry and calls LiteLLM's OpenAI-compatible API directly.

Both use the same `gpt-5.4-nano` model in a separate **source Foundry account**.
The **consumer Foundry account** hosts both agents but receives no model capacity
from this sample. Neither tutor calls the other or accesses the source model
directly.

## Architecture

### Prompt agent: Foundry-managed gateway integration

The agent selects `litellm-gateway/gpt-5.4-nano`. Foundry resolves the connection
and authenticates to LiteLLM with its stored bearer credential; LiteLLM then
authenticates to the source Foundry model using managed identity and Azure RBAC.

```mermaid
flowchart LR
    User["Learner / client"]
    subgraph Consumer["Consumer Foundry · consumer-project"]
        Tutor["ai-tutor-prompt-agent"]
        Connection["litellm-gateway<br/>ModelGateway connection"]
        Tutor -->|"litellm-gateway/gpt-5.4-nano"| Connection
    end
    Gateway["LiteLLM<br/>Azure Container Apps"]
    subgraph Source["Source Foundry"]
        Model["gpt-5.4-nano"]
    end
    User -->|"Entra authentication"| Tutor
    Connection -->|"HTTPS + bearer key"| Gateway
    Gateway -->|"Managed identity + Azure RBAC"| Model
```

### Hosted agent: direct OpenAI-compatible integration

The hosted tutor exposes the Foundry Responses protocol to clients. Its own
Python code calls LiteLLM Chat Completions with the alias `gpt-5.4-nano`, without
a ModelGateway connection or a call to the prompt tutor. Its agent identity reads
the gateway credential from Key Vault.

```mermaid
flowchart LR
    User["Learner / client"]
    subgraph Consumer["Consumer Foundry · consumer-project"]
        Hosted["ai-tutor-hosted-agent<br/>Python + Microsoft Agent Framework"]
    end
    Vault["Azure Key Vault"]
    Gateway["LiteLLM<br/>Azure Container Apps"]
    subgraph Source["Source Foundry"]
        Model["gpt-5.4-nano"]
    end
    User -->|"Entra authentication<br/>Foundry Responses protocol"| Hosted
    Hosted -.->|"Agent identity: read gateway key"| Vault
    Hosted -->|"OpenAI Chat Completions<br/>gpt-5.4-nano + bearer key"| Gateway
    Gateway -->|"Managed identity + Azure RBAC"| Model
```

Both paths share the same LiteLLM deployment, whose identity also resolves its
gateway key from Key Vault. Both Foundry projects, the hosted tutor, and LiteLLM
use shared Application Insights; supported resource diagnostics go to its Log
Analytics workspace. See [Shared monitoring](#shared-monitoring).

The diagrams above describe the **legacy default**. Entra modes replace the
client-to-gateway credential, not LiteLLM, either tutor, or the gateway's
managed-identity access to the source model. See [Incoming authentication](#incoming-authentication).

## Requirements

- Azure CLI, Azure Developer CLI 1.35+, and Bicep (`az bicep install`).
- Foundry agents azd extension (`azd extension install azure.ai.agents`).
- Bash, OpenSSL, and Node.js 18+ for deployment hooks and tests.
- Python 3.13 for local hosted-agent development (not required for remote builds).
- Subscription quota for `gpt-5.4-nano` with the `GlobalStandard` SKU.
- Permission to provision resources and create role assignments: Owner, or
  Contributor plus an appropriate Azure RBAC administrator role.
- Entra registration work is a **separate, explicitly approved** directory
  operation: the deploying identity needs application-management permissions
  (Microsoft Graph `Application.ReadWrite.All` or appropriate owned-application
  permissions and directory role). Creating service principals and granting
  consent require their corresponding tenant permissions; subscription Owner
  does not grant these. Normal resource provisioning does not provision directory
  objects or grant consent.
- Entra hosted mode needs a genuine Foundry Entra Agent ID and a runtime in
  which `DefaultAzureCredential` can obtain its gateway-audience token. A
  developer user's token or ordinary managed identity is not a substitute.
- Optional delegated operation additionally requires a trusted caller backend
  with sign-in/OBO support, approved human object IDs, approved agent application
  IDs, consent for `gateway.invoke`, and Agent ID/blueprint permissions for that
  flow. Agent-user impersonation has separate tenant prerequisites and is **not**
  implemented or accepted as human delegation here.

## Deploy

| Region | Location |
| --- | --- |
| **East US (default)** | `eastus` |
| UK South | `uksouth` |
| Sweden Central | `swedencentral` |

```bash
az login
azd auth login

# Substitute uksouth or swedencentral if needed.
azd env new litellm-demo --location eastus --subscription <subscription-id>
LITELLM_MASTER_KEY="sk-$(openssl rand -hex 32)" azd up
```

`azd up` provisions infrastructure and deploys both tutors. The prompt agent uses
lifecycle hooks; the hosted agent uses native azd code deployment with a remote
Python build, followed by a Bicep RBAC hook. No Docker or container registry is
required.

## The two tutors

| Agent | Implementation | Model selection | Deploy/update |
| --- | --- | --- | --- |
| `ai-tutor-prompt-agent` | Foundry-managed prompt | `litellm-gateway/gpt-5.4-nano` through the ModelGateway connection | `azd hooks run postprovision` |
| `ai-tutor-hosted-agent` | Python + Microsoft Agent Framework | `gpt-5.4-nano` directly against LiteLLM `/v1` | `azd deploy ai-tutor-hosted-agent` |

Both provide explanations, hints, worked examples, and practice questions.
They are separate agents in `consumer-project`, with no external tools or
cross-session memory. Deploying either does not replace the other.

### Prompt tutor

Edit [agent.json](agents/ai-tutor-prompt-agent/agent.json), then run:

```bash
azd hooks run postprovision
```

The JSON follows the standard prompt-agent layout: top-level `name`,
`description`, `kind`, `model`, and `instructions`. The prompt agent explicitly selects
its gateway model, currently `"model": "litellm-gateway/gpt-5.4-nano"`.
Changing this value selects a model; it does not provision one. The alias must
already be configured in LiteLLM and exposed by the Foundry connection.

The [deployment hook](scripts/deploy-prompt-agent.mjs) uses the signed-in Azure
CLI identity and the Bicep output `CONSUMER_FOUNDRY_PROJECT_ENDPOINT`.
It preserves the model from the agent file rather than overriding it with
`CONSUMER_MODEL_REFERENCE`. It maps the flat agent fields into the Foundry REST
request's `definition` object and calls the API (`api-version=v1`),
verifies persistence, and fails the azd command on errors. Unchanged definitions
reuse the latest version; changes create a new version.

[azure.yaml](azure.yaml) registers the same hook for `postprovision` and
`postdeploy`, so infrastructure and agent updates stay aligned. Running it twice
during `azd up` does not create duplicate versions. Agent deployment does not
read the LiteLLM key.

### Hosted tutor

[main.py](agents/ai-tutor-hosted-agent/main.py) wraps an Agent Framework
`OpenAIChatCompletionClient` with Foundry's `ResponsesHostServer`. Clients invoke
it through the Foundry Responses protocol; its own model requests go to LiteLLM
Chat Completions, not to the prompt agent.

Edit [agent.json](agents/ai-tutor-hosted-agent/agent.json) to change its tutor
instructions or LiteLLM model alias, then run `azd deploy ai-tutor-hosted-agent`.
Its model is intentionally `gpt-5.4-nano`, without the connection prefix: direct
gateway requests use LiteLLM aliases, whereas the prompt agent uses Foundry's
connection-qualified name.

The hosted identity exists only after deployment, so a small service-level
`postdeploy` hook reads that identity and applies
[hosted-agent.bicep](infra/modules/hosted-agent.bicep) to grant **Key Vault Secrets
User** on just the gateway secret and **Monitoring Metrics Publisher** on the
shared Application Insights component. The code resolves the secret lazily on
model requests, rather than at server startup. No key is included in the code
package or hosted-agent environment.
The hook does not create or deploy the agent: packaging, remote build, versioning,
and publication use the standard Foundry azd extension.

For local development, create `.venv` in
[the hosted agent folder](agents/ai-tutor-hosted-agent), then run
`azd ai agent run ai-tutor-hosted-agent --no-client` from the project root.
The command installs the pinned Python dependencies and injects the selected azd
environment. Local calls use your signed-in Azure identity. Press **F5** in
VS Code to start the debugger and open Foundry Toolkit Agent Inspector.

## Permissions and secrets

The deploying user receives explicit Azure RBAC assignments, independent of
Entra Global Administrator:

| Role | Scope |
| --- | --- |
| Contributor | Sample resource group |
| Key Vault Secrets Officer | Sample vault |
| Foundry User / Azure AI User | Both Foundry accounts |
| Log Analytics Reader | Application Insights and its workspace |
| Privileged Monitoring Data Reader | Application Insights and its workspace (protected GenAI data) |

Contributor does not permit role-assignment creation; provisioning RBAC still
requires appropriate administrator permissions. The template targets a human
deployer, not a CI service principal.

The gateway identity receives **Cognitive Services OpenAI User** on the source
account and **Key Vault Secrets User** on the vault. Both Foundry accounts disable
local API-key authentication.

In legacy mode, `LITELLM_MASTER_KEY` is the shared LiteLLM bearer/admin credential. Bicep stores
it in an RBAC-protected, purge-protected Key Vault; ACA resolves a versionless
secret reference using managed identity. The Foundry connection stores a
separate secure copy, so rotation must update both.

For infrastructure updates, reuse the existing key rather than generating one:

```bash
vault="$(azd env get-value AZURE_KEY_VAULT_NAME)"
LITELLM_MASTER_KEY="$(az keyvault secret show --vault-name "$vault" \
  --name litellm-master-key --query value -o tsv)" azd provision
```

Never commit secrets or share `.azure/` contents. Using `azd env set` for the
master key persists a local copy; the process-scoped examples above avoid that.
Production consumers should use restricted gateway credentials, not its master key.

## Incoming authentication

Legacy deployments remain `api_key` unless explicitly migrated. Gateway and
hosted authentication modes are separate settings; changing one does not safely
migrate the other. No failed Entra request falls back to a key or app identity.

| Path / mode | Identity the gateway authorizes |
| --- | --- |
| Legacy prompt / hosted `api_key` | Shared gateway key; no authenticated human attribution |
| Prompt OAuth2 client credentials | Dedicated prompt-client application's service principal (`oid`) and application ID (`azp`), **not** the human or prompt agent's Agent ID |
| Hosted `entra` | Genuine hosted Entra Agent ID service principal and agent application ID |
| Hosted `entra_delegated` | Approved human `oid` plus approved agent actor `azp`, `gateway.invoke` scope, and AgentIdentity actor facet |

[entra_auth.py](litellm/entra_auth.py) uses LiteLLM's documented OSS
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
[gateway_policy.py](litellm/gateway_policy.py):

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

The Entra launcher wraps LiteLLM in [gateway_app.py](litellm/gateway_app.py) to
remove bearer/alternate credential headers and untrusted tracing baggage
**before** LiteLLM sees request headers. The custom-auth hook still performs
authentication and authorization, returning only nonsecret validated identity.
Raw tokens are never returned as `api_key`/`token` or usage metadata.
Spend persistence and message capture are disabled. This is not a multi-tenant
governance service; it supplies no billing or per-user rate-limit guarantees.

### Hosted modes and playground

[main.py](agents/ai-tutor-hosted-agent/main.py) supports explicit `api_key`,
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

### Delegation is opt-in, not playground OBO

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

## Approval-gated Entra migration

Nothing below is run automatically by this change. Approve directory changes,
credential operations, resource updates, tutor deployments and permission
revocation separately. Keep existing identities, registration IDs and the existing
master key. Plan a coordinated cutover: one gateway authentication mode is active
at a time; Entra mode does not continue accepting legacy keys.

1. **Register or adopt directory identities separately.** Use
   [infra/entra/main.bicep](infra/entra/main.bicep), not ordinary `azd provision`.
   Its Microsoft Graph extension is pinned to GA `microsoftgraph/v1.0:1.0.0`.
   Keep a stable `registrationKey` for repeat deployments. For adoption, prepare
   a nonsecret `registration.json` containing `tenantId`, `registrationKey`,
   `gateway` and `prompt` identity objects, each with `objectId`, `clientId` and
   `principalId`; include the existing `gatewayScopeId`. Optional
   `preauthorizeAzureCli` is for controlled testing, not an authorization bypass.
   Run the approval-gated preparation script first:

   ```bash
   node scripts/prepare-entra-registration.mjs --config registration.json --approve
   ```

   Preparation validates IDs, sets a missing immutable `uniqueName`, and
   merges only the required API/scope/optional-claim settings, preserving
   unrelated manifest settings, scope IDs and credentials. Adopted applications
   are read with Graph `existing` declarations rather than redeclared manifests.
   Repeat deployments must retain the same registration key/unique names;
   service principals resolve by unchanged application IDs.

   Deploy the separate Graph template to an existing approved resource group:

   ```bash
   az deployment group create --resource-group "$AZURE_RESOURCE_GROUP" \
     --name entra-registration --template-file infra/entra/main.bicep \
     --parameters registrationKey="$REGISTRATION_KEY" \
       existingGatewayObjectId="$GATEWAY_OBJECT_ID" \
       existingPromptObjectId="$PROMPT_OBJECT_ID" \
       gatewayScopeId="$GATEWAY_SCOPE_ID"
   ```

   For **new** registrations omit the three adoption parameters. Microsoft
   Graph reads can lag a just-created application, so the first deployment can
   fail with `Request_ResourceNotFound` in the `gateway-api-identifier` step:
   wait briefly and re-run the identical command, which is idempotent. Retain the
   nonsecret application, service-principal and scope outputs. Do not create
   passwords in Bicep, replace registrations, reset credentials, or infer object
   IDs from display names. Consent and genuine hosted Agent ID permissions
   remain separate approval-gated tenant operations.

2. **Configure nonsecret policy.** Entra mode runs on the same pinned stock
   LiteLLM image: its `proxy` extra already provides PyJWT, `cryptography` and
   `httpx`, and the launcher imports them at startup, failing closed if absent.
   No custom image or runtime package installation is needed. Set `ENTRA_TENANT_ID`,
   `ENTRA_GATEWAY_CLIENT_ID`, `ENTRA_AGENT_CLIENT_ID`,
   `ENTRA_AGENT_PRINCIPAL_ID`, and `ENTRA_PROMPT_CLIENT_ID` to the preserved,
   verified identifiers. `ENTRA_APP_PRINCIPALS` is a JSON array of approved
   `{"oid":"<service-principal-id>","azp":"<application-client-id>"}` pairs,
   including the dedicated prompt client and hosted Agent ID.
   `ENTRA_DELEGATED_USERS` and `ENTRA_DELEGATED_ACTORS` are JSON arrays of GUIDs;
   leave them empty unless delegation and consent have been approved.
   Only these nonsecret settings belong in azd state.

3. **Preview, approve and apply the resource cutover.** Use
   `GATEWAY_AUTH_MODE=entra`, `HOSTED_AUTH_MODE=entra` (or explicitly
   `entra_delegated`), and `PROMPT_AUTH_MODE=oauth2`. Keep
   `ENTRA_DELEGATED_ONLY=false`: the shared deployment requires prompt app-only
   access and rejects a global delegated-only policy. `ENTRA_GATEWAY_SCOPE` is
   derived as `api://<gateway-client-id>/.default`.
   Run `azd provision --preview` with the existing process-scoped master key,
   inspect the changes, then provision only after approval. Ordinary resource
   deployment and native hosted-agent hooks remain resource/agent operations,
   not implicit directory provisioning.

4. **Explicitly configure prompt OAuth credentials and the connection.**
   Prepare a nonsecret `prompt-oauth.json` with `tenantId`, `gateway` and `prompt`
   identity objects as above, plus `subscriptionId`, `resourceGroup`,
   `consumerFoundryName`, `consumerProjectName`, `vaultName`, `secretName`,
   `litellmEndpoint`, and `modelDeploymentName` (`gpt-5.4-nano`).
   Then approve and run:

   ```bash
   node scripts/configure-prompt-oauth.mjs --config prompt-oauth.json --approve
   ```

   [configure-prompt-oauth.mjs](scripts/configure-prompt-oauth.mjs) verifies
   application/service-principal identifiers before credential operations,
   creates or reuses the dedicated application's credential in Key Vault,
   and configures ModelGateway **OAuth2 client credentials**, not
   `ProjectManagedIdentity`, using Graph, Key Vault and ARM calls. Ordinary
   provisioning skips the legacy connection module in `oauth2` mode, so it
   does not overwrite the configured OAuth connection.
   No secret belongs in source, deployment outputs,
   logs, command-line parameters, or azd state.
   Expired or mismatched credentials cause an error; they are never silently
   replaced. To rotate deliberately, repeat with `--approve --rotate`.
   Reapply/verify the connection before retiring an old credential; do not
   remove credentials still used by a legacy deployment.

5. **Approve tutor deployment and Azure acceptance testing.** Deploy the
   independent tutors with the updated settings and existing identities.
   Verify the prompt application and the genuine hosted Agent ID independently,
   both health probes, supported streaming/non-streaming playground options,
   source-model managed-identity access, and negative authorization cases.
   Verify delegation only with the trusted OBO backend and consent in place.
   Never retry a denied human request using app credentials.

6. **Revoke old hosted secret access only after verification and approval.**
   Entra hosted deployments omit new secret-read grants but retain the
   telemetry publisher grant. Incremental Bicep omission does **not** revoke
   existing assignments. Prepare a nonsecret `revoke.json` with `tenantId`,
   `subscriptionId`, `resourceGroup`, `vaultName`, `agentPrincipalId`,
   the exact `assignmentId`, the chosen `hostedAuthMode`,
   `legacyHostedEnabled:false`, and `entraVerified:true`. Then run:

   ```bash
   node scripts/revoke-hosted-key-access.mjs --config revoke.json --approve
   ```

   The scoped migration script validates the assignment before removing it.
   Do not remove a grant or credential required by an explicitly enabled legacy
   deployment. Stop sharing the master key as an inference credential after
   migration; the gateway may still need its own backend vault reference.

## Curriculum and quiz tools through the gateway (optional)

The gateway can front two tutor-specific backends, consumed by the hosted tutor. This is
**off by default** and independent of inference. Both live in [demo/](demo) and run as
**internal-only** Container Apps in the gateway's environment, built remotely in a small
Azure Container Registry (admin user disabled, `AcrPull` via a dedicated identity):

- **`curriculum` MCP server** (read-only, stateless): `list_topics`, `get_topic`,
  `get_section(depth)` and `list_learning_modes`. Three topics (fractions, prime numbers,
  linear equations), each with three sections and three depths (`intro`, `working`,
  `deep`), stored as validated JSON. Learning modes: `guided_path`, `map_then_zoom`,
  `mastery_gate`.
- **`quiz_master` A2A 1.0 agent**: creates (`diagnostic`, `section`, `mastery`) and grades
  quizzes. **Answers, scores and pass/fail are computed in code**; a quiz id is a short
  (24-character), signed, stateless token a model can copy exactly, so no database is
  needed and answers never leave the agent. A language model reached **through this gateway** (with the quiz agent's own managed identity)
  may only reword questions and write feedback; any failure falls back to deterministic
  text, and the reply says whether the model contributed (`model_assisted`).

```
hosted tutor ──Agent ID token──▶ LiteLLM gateway ──▶ curriculum MCP   (internal)
                                      ├────────────▶ quiz_master A2A 1.0 (internal)
              quiz agent ──its own managed identity──▶ gateway ──▶ model
```

The tutor asks the learner what they want to learn, how deep to go and how they like to
learn, offers a learning mode, teaches one section at a time at the chosen depth, and
offers a quiz at the end of each section. It must not state quiz answers itself.
Learner progress is **not stored yet**; durable memory is a planned next step.
The hosted tutor uses these tools when `GATEWAY_TOOLS=enabled`; the prompt tutor uses them
when `PROMPT_TOOLS=enabled` (see [Prompt tutor tools](#prompt-tutor-tools-optional)).

**Access is per approved application, not per key.** `ENTRA_CAPABILITIES` maps an approved
principal's object ID to the MCP servers (with a per-tool allowlist) and A2A agents it may
reach. The auth hook turns that into LiteLLM's `object_permission`, which the pinned
runtime enforces without a database. Principals with no grant, the quiz agent's own
principal (inference only) and delegated user-via-agent tokens get 403 on these routes.
Backends receive no caller credential. The quiz agent's identity is approved for inference
automatically when the demo is enabled; nothing else needs configuring.

Enable it (all approval-gated; the demo needs the Entra gateway):

```bash
azd env set ENABLE_DEMO_BACKENDS true
azd env set ENTRA_CAPABILITIES '{"<hosted-agent-oid>":{"mcp":{"curriculum":["list_topics","get_topic","get_section","list_learning_modes"]},"agents":["quiz_master"]}}'
azd env set GATEWAY_TOOLS enabled        # hosted tutor opt-in; requires LITELLM_AUTH_MODE=entra
azd provision                            # registry, identities, internal apps, gateway policy
azd deploy demo-mcp                      # one service per command; builds remotely in the registry
azd deploy demo-a2a
azd deploy ai-tutor-hosted-agent         # new version with the tool code
```

The quiz signing key is generated at provision time into a Container App secret (it is
not an output and never reaches azd state); each provision rotates it, which only
invalidates quizzes that are still open.

**A2A 1.0 only.** The gateway requires `A2A-Version: 1.x` and the PascalCase method
`SendMessage`; the legacy 0.3 `message/send` and `/.well-known/agent.json` are refused.
The agent card is `/.well-known/agent-card.json`. Each tutor call is a single blocking
message with no streaming or tasks.

The hosted tutor connects to MCP lazily and tolerates an unavailable gateway (it keeps
answering without tools, retrying every 30 s). Its credential is attached only to the
gateway origin, redirects are off, and prompts are never requested. Guidance for using
the tools is added in code only when they are enabled, so both tutors keep identical
`agent.json` instructions.

**Limits.** The backends are unauthenticated and rely on internal ingress, so any other
app added to the same environment could reach them. Onward authentication from the
gateway to real backends is not solved here: LiteLLM's MCP and A2A client auth uses static
headers, OAuth client secrets, or token exchange, and for Foundry A2A agents only
credentials on the agent entry (not the gateway's managed identity). There are no per-tool
budgets or rate limits and no admin UI. The quiz id embeds its random seed, so anyone with
the agent's source code and a quiz id could derive the answers; the signature stops
tampering, not reading. Learners are not distinguished from each other (the gateway
authorizes the agent, not the user), which is why per-learner memory needs a trusted
learner id first. Azure Functions can host MCP servers (the MCP extension), but I found no
released A2A support there, so both backends use Container Apps. The gateway logs one
`Error listing tools` warning at startup if it starts before the backends do; calls worked
afterwards in testing.

### Prompt tutor tools (optional)

`PROMPT_TOOLS=enabled` makes [deploy-prompt-agent.mjs](scripts/deploy-prompt-agent.mjs) (run by the
`postprovision` and `postdeploy` hooks) give the Foundry prompt agent the same two backends:

- the **curriculum MCP server** through a Foundry **toolbox** (`tutor-tools`, MCP tool pointing at
  the gateway's `/mcp/curriculum`), reached by the agent through a `UserEntraToken` connection;
- the **quiz agent** as a **direct A2A tool** (`/a2a/quiz_master/`, A2A 1.0, card credentials sent).
  A toolbox cannot carry it: Foundry fetches a toolbox A2A card anonymously and the gateway protects it.

The script creates the three project connections (`gateway-mcp`, `gateway-a2a`, `tutor-tools-conn`),
the toolbox, and a new agent version whose instructions swap the "no external tools" sentence for
tool guidance (it spells out the exact JSON the quiz agent expects, because Foundry lets the model
write free text). Everything is idempotent: unchanged resources are not rewritten and no new
agent version is created when nothing changed.

The gateway sees the prompt agent's **own agent identity** (not the OAuth client the model calls
use), so that identity must be approved first. **Tools are only attached once it is approved**;
until then the plain tutor is deployed and the script prints what to do. This matters because a
tool that Foundry cannot reach (gateway denial, outage) makes the **whole request fail with 400**,
not just the tool call.

```bash
azd env set ENABLE_DEMO_BACKENDS true                 # backends must exist (see above)
azd env set PROMPT_TOOLS enabled
azd deploy ai-tutor-hosted-agent                      # or any deploy: the hook runs and prints the agent identity
# approve that identity, keeping the hosted agent's existing entries:
azd env set ENTRA_APP_PRINCIPALS '[...existing..., {"oid":"<id>","azp":"<id>"}]'
azd env set ENTRA_CAPABILITIES '{...existing..., "<id>":{"mcp":{"curriculum":["list_topics","get_topic","get_section","list_learning_modes"]},"agents":["quiz_master"]}}'
LITELLM_MASTER_KEY="$(az keyvault secret show --vault-name "$(azd env get-value AZURE_KEY_VAULT_NAME)" --name litellm-master-key --query value -o tsv)" azd provision
az containerapp revision restart ...                  # only needed when gateway config files changed
```

The operator needs Foundry Project Manager (to create connections) and Foundry User on the consumer project.
To remove the tools set `PROMPT_TOOLS=disabled` and deploy again; the connections and toolbox
are left in place (delete them in the portal or with the ARM/Foundry APIs if unwanted).

### Prompt agents and Foundry tools: spike findings

A throwaway prompt agent, a Foundry toolbox and `RemoteTool`/`RemoteA2A` project connections
with `AgenticIdentityToken` auth (audience `api://<gateway client id>`) were pointed at this
gateway, then deleted. The spike scripts are not part of this repo. Observed live:

| Question | Finding |
| --- | --- |
| Which identity does the gateway see for tools? | The **prompt agent's own instance identity** (`oid` = `azp`, `idtyp` app), a distinct one **per agent**. Model calls arrive separately as the ModelGateway OAuth2 client. Each agent identity must be added to `ENTRA_APP_PRINCIPALS` and `ENTRA_CAPABILITIES` before it can use tools. |
| Can the toolbox be probed with a developer token? | No. An `AgenticIdentityToken` connection needs an agent identity context, so `tools/list` with a user token fails. Only an agent run exercises it. |
| MCP through the toolbox | **Works.** The model (through LiteLLM) chose a tool, Foundry ran it against the gateway and returned the result. Tool names are prefixed (`curriculum___curriculum-list_topics`). The MCP client also sends `DELETE` to close sessions; the route policy denies it with 403, which is harmless. |
| A2A through the toolbox | **Does not work with the gateway's protected agent card.** The toolbox rejects an A2A tool that sets both `base_url` and `project_connection_id`, and without `base_url` it fetches the card anonymously (`reason=no_token`), so `send_credentials_for_agent_card` has no effect. |
| A2A as a direct agent tool (no toolbox) | **Works end to end** with `base_url` plus `project_connection_id` and `send_credentials_for_agent_card: true`: authenticated card fetch, one `SendMessage` with a 1.x `A2A-Version`, and the quiz agent's structured reply. |
| Failure behaviour | A tool source that fails (403, 401, bad card) makes the **whole request return 400**, so one broken tool takes down the agent's plain answers too. A misconfigured source was silently left out of the tool list once. |

Gateway and setup requirements the spike exposed (the first two are fixed in this repo):

- The agent card must contain `supportedInterfaces` (Foundry's A2A 1.0 parser requires it), and its URL must be **https**. The gateway now serves both: [config.demo.yaml](litellm/config.demo.yaml) declares the interface and [serve.py](litellm/serve.py) trusts the ingress `X-Forwarded-*` headers so the public URL is `https://`.
- Config and Python sources reach the gateway as mounted secrets, so changing them with `azd provision` does **not** create a revision: restart the active revision (`az containerapp revision restart`).
- The A2A connection `target` needs a **trailing slash** (`.../a2a/quiz_master/`), otherwise Foundry resolves the card at `/a2a/.well-known/agent-card.json`.
- The MCP protocol version Foundry requires is `2025-11-25`.
- Foundry's A2A tool lets the model write free text. `quiz_master` only accepts a JSON action message, so a prompt agent needs instructions that spell out the exact JSON (it worked when told to), or the quiz agent needs a natural-language front door.
- Not done: A2A through a toolbox with an anonymous card (needs a deliberate gateway policy exception), approvals (`require_approval: always`), and a user-on-agent or delegated token.

## Shared monitoring

[monitoring.bicep](infra/modules/monitoring.bicep) creates one workspace-backed
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

### Live verification (one test environment)

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

## Current azd Foundry extension limitation

Foundry requires **`<connection-name>/<model-name>`** for a gateway-connected
prompt agent, here `litellm-gateway/gpt-5.4-nano`.

The tested `azure.ai.agents` extension (`1.0.0-beta.18`) rejects `/` in the
model field during native `azd deploy` validation:

```text
model deployment name "litellm-gateway/gpt-5.4-nano" is not valid
```

This prevents using the gateway reference directly in a native prompt
`azure.ai.agent` service. Quotes or escaping do not help: validation checks the parsed string.
Renaming the model or connection cannot remove Foundry's required separator.

**Workaround:** this sample uses azd hooks to deploy the prompt agent through the
supported Foundry API, preserving the exact gateway reference. This is an
extension validation limitation, not a Foundry or LiteLLM limitation. It does not
affect the native hosted deployment: Python selects a LiteLLM alias at runtime
instead of declaring a Foundry model reference in the service manifest.

See [Foundry gateway-connected models](https://learn.microsoft.com/azure/foundry/agents/how-to/ai-gateway#create-a-prompt-agent-with-the-model-connection)
and [azd lifecycle hooks](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-extensibility).
