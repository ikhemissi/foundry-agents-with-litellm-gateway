import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

from agent_framework import Agent
from agent_framework.observability import enable_instrumentation
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.monitor.opentelemetry import configure_azure_monitor
from openai import AsyncOpenAI


def create_agent(
    base_url: str,
    get_api_key: Callable[[], Awaitable[str]],
    definition_path: Path = Path(__file__).with_name("agent.json"),
) -> Agent:
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.path.rstrip("/") != "/v1"
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("LITELLM_BASE_URL must be an HTTPS LiteLLM /v1 endpoint.")
    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    for field in ("name", "model", "instructions"):
        if not isinstance(definition.get(field), str) or not definition[field].strip():
            raise ValueError(f"agent.json requires a nonempty {field}.")
    client = OpenAIChatCompletionClient(
        model=definition["model"],
        async_client=AsyncOpenAI(base_url=base_url, api_key=get_api_key),
    )
    return Agent(
        client=client,
        name=definition["name"],
        instructions=definition["instructions"],
    )


async def read_gateway_key(vault: SecretClient) -> str:
    # Resolve lazily: the identity receives RBAC after the hosted version exists.
    secret = await asyncio.to_thread(vault.get_secret, "litellm-master-key")
    if not secret.value:
        raise RuntimeError("Key Vault returned an empty litellm-master-key.")
    return secret.value


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    with DefaultAzureCredential() as credential, SecretClient(
        vault_url=f"https://{os.environ['AZURE_KEY_VAULT_NAME']}.vault.azure.net",
        credential=credential,
    ) as vault:
        configure_azure_monitor(
            connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"],
            credential=credential,
            enable_live_metrics=False,
        )
        enable_instrumentation(enable_sensitive_data=False, enable_message_events=False)

        async def get_api_key() -> str:
            return await read_gateway_key(vault)

        agent = create_agent(os.environ["LITELLM_BASE_URL"], get_api_key)
        # Keep the explicit Entra-authenticated exporter; do not configure it twice.
        ResponsesHostServer(agent, configure_observability=None).run()


if __name__ == "__main__":
    main()
