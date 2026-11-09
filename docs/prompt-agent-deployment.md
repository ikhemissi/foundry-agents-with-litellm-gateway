# How the prompt agent is deployed

Foundry refers to a gateway-connected model as **`<connection-name>/<model-name>`**, here
`litellm-gateway/gpt-5.4-nano`.

The azd agents extension (`azure.ai.agents` `1.0.0-beta.18`, October 2026) validates the model field of a native
prompt `azure.ai.agent` service as a model deployment name, which does not contain `/`:

```text
model deployment name "litellm-gateway/gpt-5.4-nano" is not valid
```

This sample therefore deploys the prompt agent through the Foundry API from an azd hook
([deploy-prompt-agent.mjs](../scripts/deploy-prompt-agent.mjs)), keeping the exact gateway reference. The hosted agent
is deployed natively: its Python code selects a LiteLLM alias at runtime instead of declaring a Foundry model
reference in the service manifest.

See [Foundry gateway-connected models](https://learn.microsoft.com/azure/foundry/agents/how-to/ai-gateway#create-a-prompt-agent-with-the-model-connection)
and [azd lifecycle hooks](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-extensibility).
