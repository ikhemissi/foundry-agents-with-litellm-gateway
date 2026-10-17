import { approve, azureClient, cli, guid } from './identity-common.mjs';

export async function revokeHostedKeyAccess(config, { approved, arm }) {
  approve(approved);
  guid(config.tenantId, 'tenantId');
  guid(config.subscriptionId, 'subscriptionId');
  const principalId = guid(config.agentPrincipalId, 'agentPrincipalId');
  guid(config.assignmentId, 'assignmentId');
  if (!['entra', 'entra_delegated'].includes(config.hostedAuthMode)
      || config.legacyHostedEnabled !== false || config.entraVerified !== true) {
    throw new Error('Confirm Entra is verified and no hosted legacy deployment needs this assignment.');
  }
  if (!/^[A-Za-z0-9_.()-]+$/.test(config.resourceGroup ?? '')
      || !/^[a-zA-Z0-9-]+$/.test(config.vaultName ?? '')) throw new Error('Invalid scope.');
  const scope = `/subscriptions/${config.subscriptionId}/resourceGroups/${config.resourceGroup}`
    + `/providers/Microsoft.KeyVault/vaults/${config.vaultName}/secrets/litellm-master-key`;
  const path = `${scope}/providers/Microsoft.Authorization/roleAssignments/${config.assignmentId}?api-version=2022-04-01`;
  const assignment = await arm('GET', path, undefined, { allow404: true });
  if (!assignment) return { removed: false };
  if (assignment.properties?.principalId?.toLowerCase() !== principalId
      || assignment.properties?.scope?.toLowerCase() !== scope.toLowerCase()
      || assignment.properties?.roleDefinitionId?.toLowerCase()
        !== `/subscriptions/${config.subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6`.toLowerCase()) {
    throw new Error('Assignment principal, scope or role differs; refusing deletion.');
  }
  await arm('DELETE', path);
  if (await arm('GET', path, undefined, { allow404: true })) throw new Error('Assignment deletion was not persisted.');
  return { removed: true };
}

cli(import.meta.url, async (config, options) => revokeHostedKeyAccess(config, {
  ...options,
  arm: azureClient(config.tenantId, 'https://management.azure.com', 'https://management.azure.com'),
}));
