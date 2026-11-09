# Curriculum and quiz tools through the gateway (optional)

The gateway can front two tutor-specific backends, consumed by the hosted tutor. This is
**off by default** and independent of inference. Both live in [demo/](../demo) and run as
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

## Prompt tutor tools (optional)

`PROMPT_TOOLS=enabled` makes [deploy-prompt-agent.mjs](../scripts/deploy-prompt-agent.mjs) (run by the
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
until then the plain tutor is deployed and the script prints what to do. When a tool source cannot be
reached (for example, the gateway does not allow the agent yet), the whole request returns HTTP 400 (observed in
October 2026): attach tools only once the gateway allows the agent.

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

## Prompt agents and Foundry tools: spike findings

A throwaway prompt agent, a Foundry toolbox and `RemoteTool`/`RemoteA2A` project connections
with `AgenticIdentityToken` auth (audience `api://<gateway client id>`) were pointed at this
gateway, then deleted. The spike scripts are not part of this repo. Observed live:

| Question | Finding |
| --- | --- |
| Which identity does the gateway see for tools? | The **prompt agent's own instance identity** (`oid` = `azp`, `idtyp` app), a distinct one **per agent**. Model calls arrive separately as the ModelGateway OAuth2 client. Each agent identity must be added to `ENTRA_APP_PRINCIPALS` and `ENTRA_CAPABILITIES` before it can use tools. |
| Can the toolbox be probed with a developer token? | No. An `AgenticIdentityToken` connection needs an agent identity context, so `tools/list` with a user token fails. Only an agent run exercises it. |
| MCP through the toolbox | **Works.** The model (through LiteLLM) chose a tool, Foundry ran it against the gateway and returned the result. Tool names are prefixed (`curriculum___curriculum-list_topics`). The MCP client also sends `DELETE` to close sessions; the route policy denies it with 403, which is harmless. |
| A2A through the toolbox | Use a direct A2A tool when the agent card requires credentials: a toolbox A2A tool takes either `base_url` or `project_connection_id`, and without `base_url` it fetches the card without credentials (`reason=no_token`), so `send_credentials_for_agent_card` does not apply there. |
| A2A as a direct agent tool (no toolbox) | **Works end to end** with `base_url` plus `project_connection_id` and `send_credentials_for_agent_card: true`: authenticated card fetch, one `SendMessage` with a 1.x `A2A-Version`, and the quiz agent's structured reply. |
| Failure behaviour | A tool source that returns an error (403, 401, an invalid card) makes the whole request return HTTP 400, plain answers included; once, a misconfigured source was left out of the tool list. Attach a tool only once its source accepts the agent. |

Gateway and setup requirements the spike exposed (the first two are fixed in this repo):

- The agent card must contain `supportedInterfaces` (Foundry's A2A 1.0 parser requires it), and its URL must be **https**. The gateway now serves both: [config.demo.yaml](../litellm/config.demo.yaml) declares the interface and [serve.py](../litellm/serve.py) trusts the ingress `X-Forwarded-*` headers so the public URL is `https://`.
- Config and Python sources reach the gateway as mounted secrets, so changing them with `azd provision` does **not** create a revision: restart the active revision (`az containerapp revision restart`).
- The A2A connection `target` needs a **trailing slash** (`.../a2a/quiz_master/`), otherwise Foundry resolves the card at `/a2a/.well-known/agent-card.json`.
- The MCP protocol version Foundry requires is `2025-11-25`.
- Foundry's A2A tool lets the model write free text. `quiz_master` only accepts a JSON action message, so a prompt agent needs instructions that spell out the exact JSON (it worked when told to), or the quiz agent needs a natural-language front door.
- Not done: A2A through a toolbox with an anonymous card (needs a deliberate gateway policy exception), approvals (`require_approval: always`), and a user-on-agent or delegated token.
