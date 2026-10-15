# LiteLLM on Azure Container Apps with Microsoft Foundry

Infrastructure-only Azure Developer CLI (`azd`) and Bicep example:

```text
Client -- HTTPS + bearer key --> LiteLLM / Azure Container Apps
                                      |
                                      | managed identity + Azure RBAC
                                      v
                               Source Foundry account
                                 source-project
                                 gpt-5.4-nano deployment

Consumer Foundry account
  consumer-project
  litellm-gateway ModelGateway connection --> LiteLLM
  No local model capacity or agents
```

The two Foundry accounts are independent resources, not just two projects in
one account. Model capacity is deployed only in the source account. The consumer
registers an admin-connected model through LiteLLM without provisioning another
copy of the model.

## Prerequisites

- Azure CLI, Azure Developer CLI, and Bicep (`az bicep install`).
- An Azure subscription with GPT-5.4-nano **GlobalStandard** quota in the
  selected region.
- Permission to create resources and role assignments (for example,
  Contributor plus Role Based Access Control Administrator on the deployment
  scope, or Owner).
- Bash and OpenSSL for the commands below. Node.js 18+ is needed only for the
  local infrastructure tests.

## Infrastructure modules

[main.bicep](infra/main.bicep) is the subscription-scoped entry point. It creates
and tags the resource group, directly orchestrates the resource-group-scoped
modules, and exposes their outputs:

| Module | Resources and permissions |
| --- | --- |
| [resource-group-access](infra/modules/resource-group-access.bicep) | Deployer Contributor role scoped to this resource group |
| [identity](infra/modules/identity.bicep) | LiteLLM user-assigned identity |
| [source-foundry](infra/modules/source-foundry.bicep) | Source account, model, project, gateway model access, deployer Foundry role |
| [consumer-foundry](infra/modules/consumer-foundry.bicep) | Consumer account, project, deployer Foundry role |
| [key-vault](infra/modules/key-vault.bicep) | Vault, secret, gateway Secrets User and deployer Secrets Officer roles |
| [monitoring](infra/modules/monitoring.bicep) | Log Analytics workspace |
| [container-apps](infra/modules/container-apps.bicep) | ACA environment and LiteLLM app |
| [model-connection](infra/modules/model-connection.bicep) | Consumer project's gateway connection |

Every module accepts the deployment `tags` object; connection resources and role
assignments do not support tags. Resource-specific RBAC stays with its owning module. The shared
name suffix is computed once from the subscription and resource group, preserving
resource names across module refactors. Module dependencies ensure model and
secret permissions are deployed before ACA, and ACA before the model connection.
The workspace shared key is read inside the ACA module, not passed as an output.

## Choose a region and provision

Supported locations are deliberately restricted in Bicep:

| Display name | Azure location |
| --- | --- |
| **East US (default)** | `eastus` |
| UK South | `uksouth` |
| Sweden Central | `swedencentral` |

```bash
az login
azd auth login

# Use eastus by default. Substitute uksouth or swedencentral to select another.
azd env new litellm-demo --location eastus --subscription <subscription-id>
azd env set LITELLM_MASTER_KEY "sk-$(openssl rand -hex 32)"
azd up
```

There is no application build or `azd deploy` step: Bicep deploys a pinned
public LiteLLM image and mounts [the proxy configuration](litellm/config.yaml)
through a Container Apps secret volume. `azd up` provisions the infrastructure.
Docker and Azure Container Registry are not required.

### Deployment tags

All Bicep modules accept a `tags` object. Set tag values in
[main.parameters.json](infra/main.parameters.json), not in Bicep. The current
test configuration supplies `SecurityControl=Ignore` and `CostControl=Ignore`.
These are environment-specific governance tags, not recommended production
defaults; use them only where your organization's policies authorize them.
The root module preserves the required `azd-env-name` tag and forwards the
merged object to the resource group and resource module. Every taggable
resource, including the Key Vault secret, receives the merged tags. Azure
resource types without tag support (role assignments, model deployments, and
Foundry connections)
cannot carry these tags.

Tags alone do not guarantee a policy exemption. Verify the resulting resource
configuration and policy evaluation after provisioning.

East US is also the Bicep default and the parameter-file fallback when
`AZURE_LOCATION` is unset. To change a region **before first provisioning**:

```bash
azd env set AZURE_LOCATION uksouth
```

Use a new azd environment rather than moving an existing deployment to another
region; resource-group locations cannot be changed in place.

The default model is `gpt-5.4-nano`, version `2026-03-17`, with a
`GlobalStandard` SKU and capacity `10` (10,000 tokens/minute). Availability was
verified through the Azure model catalog for all three regions on 2026-10-15.
Availability and subscription quota can change. Check your target region:

```bash
az cognitiveservices model list --location eastus \
  --query "[?model.name=='gpt-5.4-nano'].{name:model.name,version:model.version,skus:model.skus[].name}"
```

**GlobalStandard does not guarantee inference stays in the selected region.**
The selected region controls resource placement; inference can be served
globally. This sample does not implement a regional data-residency guarantee.

Adjust capacity before provisioning if needed:

```bash
azd env set MODEL_CAPACITY 10
```

## Validate and inspect

Local compile/lint and infrastructure contract tests (no Azure deployment):

```bash
node --test tests/infra.test.mjs
```

Preview changes against Azure (requires authentication and subscription access):

```bash
azd provision --preview
```

After provisioning:

```bash
azd env get-values
```

Outputs include `LITELLM_ENDPOINT`, source/consumer Foundry account names and
project endpoints, the source Azure OpenAI endpoint, and the model deployment
name, plus `AZURE_KEY_VAULT_NAME`. No credentials are emitted as Bicep outputs.

Optional manual smoke test from Bash, after loading the selected environment:

```bash
export LITELLM_ENDPOINT="$(azd env get-value LITELLM_ENDPOINT)"
export LITELLM_MASTER_KEY="$(azd env get-value LITELLM_MASTER_KEY)"
curl --fail-with-body "${LITELLM_ENDPOINT}/v1/chat/completions" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4-nano","messages":[{"role":"user","content":"Say hello in one sentence."}]}'
```

The deploying user (`AZURE_PRINCIPAL_ID`, resolved by azd) receives:

| Role | Scope | Purpose |
| --- | --- | --- |
| Contributor | This resource group | Manage the sample resources |
| Key Vault Secrets Officer | This vault | Read, create, and rotate secrets |
| Azure AI User | Each Foundry account | Access projects and model data-plane APIs |

These are Azure RBAC assignments, independent of the Entra Global Administrator
directory role. Contributor does not grant role-assignment management; initial
and subsequent provisioning that creates RBAC assignments still requires Owner
or an appropriate Azure RBAC administrator role. The template targets a human
deployer (`principalType: User`), not a CI service principal.

LiteLLM receives **Cognitive Services OpenAI User**, scoped to the source account.
It authenticates with a user-assigned managed identity and refreshes Entra tokens
automatically. Both Foundry accounts have local API-key authentication disabled.
The consumer project's ModelGateway connection authenticates to LiteLLM using
the gateway key. It does not grant the consumer direct access to the source.

## Consumer admin-connected model

In the consumer Foundry portal, open **Manage > Resource details >
Admin-connected models** to inspect `litellm-gateway`. The Bicep-managed
project connection uses:

- The deployed LiteLLM HTTPS endpoint with `/v1` as its base path.
- API-key credentials with `Authorization: Bearer {api_key}`.
- A static model list containing `gpt-5.4-nano`.
- `deploymentInPath=false` and no inference API-version query parameter.

The model reference for a future Foundry prompt agent is
`litellm-gateway/gpt-5.4-nano` (also output as `CONSUMER_MODEL_REFERENCE`).
No agent is created by this project. This feature is documented for prompt
agents through the Agent SDK; do not assume all playgrounds or direct
Responses API model calls support it.

The connection stores its own secure credential copy; it is not a live Key
Vault secret reference. When rotating the gateway key, update the connection
with the same secure deployment parameter as well.

To reprovision an existing environment without rotating its key or writing
it into the local azd environment:

```bash
vault="$(azd env get-value AZURE_KEY_VAULT_NAME)"
LITELLM_MASTER_KEY="$(az keyvault secret show --vault-name "$vault" \
  --name litellm-master-key --query value -o tsv)" azd provision
```

This requires the deployer's vault secret access and network connectivity.
The sample uses the gateway master key; production deployments should use
a restricted gateway credential instead.

## Sample boundaries and operations

- This is a public-network demonstration, not a production landing zone.
  ACA ingress requires HTTPS and a LiteLLM bearer key for inference; Foundry
  endpoints are public but require Entra authentication. Liveness/readiness
  endpoints are deliberately accessible to ACA probes.
- The bearer key is passed as a secure Bicep parameter and stored in Key Vault.
  ACA uses a versionless Key Vault secret reference, resolved by its managed
  identity with **Key Vault Secrets User** scoped to this dedicated vault.
  LiteLLM still receives the key through its environment variable.
  Key Vault uses RBAC, soft delete (7 days), and purge protection. Its public
  endpoint is enabled for this sample; access still requires authorization.
- `azd env set LITELLM_MASTER_KEY` also stores a local bootstrap copy in the
  ignored `.azure/` directory. To avoid that persistent azd environment copy,
  supply `LITELLM_MASTER_KEY` as a process environment variable when provisioning,
  for example `LITELLM_MASTER_KEY="sk-$(openssl rand -hex 32)" azd provision`.
  Keep the same key available securely for later provisions; do not generate a
  new key on every redeployment unless intentionally rotating it. Never commit
  or share `.azure/` contents or `azd env get-values` output.
- The deployer receives Secrets Officer on this vault. Other users do not
  receive secret access automatically. Do not grant this role to all consumers
  or use the master key as a per-user API key.
- One replica, 1 vCPU, 2 GiB memory, and no Postgres/Redis. This illustrates
  stateless routing with a shared master key, not virtual keys, persistent spend
  tracking, gateway administration, or high availability. One replica remains
  running and incurs costs.
- Log Analytics retains ACA console/system logs for 30 days. Do not enable
  verbose request/prompt logging with sensitive data.
- A role assignment can take a few minutes to propagate. If inference initially
  returns an authorization error, retry after propagation and inspect ACA logs.
- LiteLLM is pinned to `v1.105.1`. Review release/security notes before upgrades.
  Changes to Bicep or the mounted configuration require `azd provision`.
  ACA secret updates alone do not restart an existing revision; after changing
  the key or configuration on an existing deployment, restart its active
  revision using `az containerapp revision restart`.
- For Key Vault key rotation, create a new secret version. ACA checks versionless
  references for updates within 30 minutes and restarts active revisions using
  the secret in environment variables. Keep the deployment parameter in sync
  so a later provision does not restore an old value.
- Model version auto-upgrades are disabled for reproducibility. Review model
  lifecycle notices and update the version before retirement.

Delete the sample and all resources in its resource group when finished:

```bash
azd down
```

Purge protection prevents immediate permanent deletion of a deleted vault;
its name remains reserved during the retention period. Use a new environment
name for clean retries rather than attempting to purge a protected vault.

## Recovering from provisioning failures

- `RequestConflict` on the source Foundry can occur when project creation and
  model deployment run concurrently against the same account. The template
  orders model deployment before source project creation to prevent that race.
  Avoid overlapping `azd up`, `azd provision`, and `azd down` operations.
- `ManagedEnvironmentCapacityHeavyUsageError` / `AKSCapacityHeavyUsage` while
  creating the ACA environment indicates Azure regional capacity pressure, not
  a model quota or LiteLLM configuration problem. The AKS wording comes from
  the managed platform; this template does not create a user-managed AKS cluster.
  Retry later or use one of the other supported regions. Model availability
  does not guarantee ACA environment capacity.

If cleaning up a failed deployment with `azd down`, let cleanup finish before
starting another deployment. To retry in UK South with new resource names:

```bash
azd env new litellm-demo-uksouth --location uksouth \
  --subscription f345fe6d-12f3-48d1-a2c3-d8c5e8b7b421
azd env set LITELLM_MASTER_KEY "sk-$(openssl rand -hex 32)"
azd up
```

Sweden Central (`swedencentral`) is also supported; neither alternate region
is guaranteed to have capacity at deployment time. East US remains the default.
A new environment avoids reusing the old resource group's location and
soft-deleted Foundry account names. If reusing an old environment produces a
soft-delete/name conflict, inspect the deleted resources before deciding whether
to recover or purge them; do not purge automatically.

## References

- [Azure Container Apps](https://learn.microsoft.com/azure/container-apps/overview)
- [Foundry model availability](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability)
- [LiteLLM Azure authentication](https://docs.litellm.ai/docs/providers/azure)
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/overview)
- [Foundry gateway-connected models](https://learn.microsoft.com/azure/foundry/agents/how-to/ai-gateway)
