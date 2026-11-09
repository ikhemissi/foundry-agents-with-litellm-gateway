# Approval-gated Entra migration

Nothing below is run automatically by this change. Approve directory changes,
credential operations, resource updates, tutor deployments and permission
revocation separately. Keep existing identities, registration IDs and the existing
master key. Plan a coordinated cutover: one gateway authentication mode is active
at a time; Entra mode does not continue accepting legacy keys.

1. **Register or adopt directory identities separately.** Use
   [infra/entra/main.bicep](../infra/entra/main.bicep), not ordinary `azd provision`.
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

   [configure-prompt-oauth.mjs](../scripts/configure-prompt-oauth.mjs) verifies
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
