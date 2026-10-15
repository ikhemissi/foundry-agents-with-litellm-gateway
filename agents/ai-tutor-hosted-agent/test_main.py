import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from azure.core.exceptions import HttpResponseError
from azure.keyvault.secrets import KeyVaultSecret, SecretClient

from main import create_agent, main, read_gateway_key


async def gateway_key() -> str:
    return "test-key"


class AgentTests(unittest.TestCase):
    def test_uses_litellm_chat_completions_and_its_own_tutor_definition(self) -> None:
        definition = json.loads(Path(__file__).with_name("agent.json").read_text())
        with (
            patch("main.AsyncOpenAI", autospec=True) as openai,
            patch("main.OpenAIChatCompletionClient", autospec=True) as chat,
            patch("main.Agent", autospec=True) as agent,
        ):
            result = create_agent("https://gateway.example/v1", gateway_key)
        openai.assert_called_once_with(
            base_url="https://gateway.example/v1", api_key=gateway_key
        )
        chat.assert_called_once_with(
            model="gpt-5.4-nano", async_client=openai.return_value
        )
        agent.assert_called_once_with(
            client=chat.return_value,
            name="ai-tutor-hosted-agent",
            instructions=definition["instructions"],
        )
        self.assertIs(result, agent.return_value)

    def test_rejects_invalid_gateway_endpoints_before_creating_a_client(self) -> None:
        for url in (
            "http://gateway.example/v1",
            "https://gateway.example/",
            "https://gateway.example/v1?api_key=bad",
            "https://user:pass@gateway.example/v1",
            "https:///v1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                create_agent(url, gateway_key)

    def test_rejects_missing_definition_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.json"
            for field in ("name", "model", "instructions"):
                definition = {
                    "name": "ai-tutor-hosted-agent",
                    "model": "gpt-5.4-nano",
                    "instructions": "Tutor the learner.",
                }
                definition[field] = ""
                path.write_text(json.dumps(definition))
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                    create_agent("https://gateway.example/v1", gateway_key, path)

    def test_configures_one_entra_authenticated_exporter_without_message_capture(self) -> None:
        with (
            patch.dict("os.environ", {
                "AZURE_KEY_VAULT_NAME": "test-vault",
                "LITELLM_BASE_URL": "https://gateway.example/v1",
                "APPLICATIONINSIGHTS_CONNECTION_STRING": "test-connection",
            }),
            patch("main.DefaultAzureCredential", autospec=True) as credentials,
            patch("main.SecretClient", autospec=True),
            patch("main.configure_azure_monitor") as monitor,
            patch("main.enable_instrumentation") as instrumentation,
            patch("main.create_agent") as agent,
            patch("main.ResponsesHostServer", autospec=True) as server,
        ):
            main()
        monitor.assert_called_once_with(
            connection_string="test-connection",
            credential=credentials.return_value.__enter__.return_value,
            enable_live_metrics=False,
        )
        instrumentation.assert_called_once_with(
            enable_sensitive_data=False, enable_message_events=False
        )
        server.assert_called_once_with(
            agent.return_value, configure_observability=None
        )
        server.return_value.run.assert_called_once()


class CredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_versionless_secret_for_each_model_request(self) -> None:
        vault = Mock(spec=SecretClient)
        vault.get_secret.side_effect = [
            KeyVaultSecret("litellm-master-key", "first-key"),
            KeyVaultSecret("litellm-master-key", "rotated-key"),
        ]
        self.assertEqual(await read_gateway_key(vault), "first-key")
        self.assertEqual(await read_gateway_key(vault), "rotated-key")
        self.assertEqual(vault.get_secret.call_count, 2)
        vault.get_secret.assert_called_with("litellm-master-key")

    async def test_surfaces_key_vault_authorization_errors(self) -> None:
        vault = Mock(spec=SecretClient)
        vault.get_secret.side_effect = HttpResponseError("Forbidden")
        with self.assertRaisesRegex(HttpResponseError, "Forbidden"):
            await read_gateway_key(vault)

    async def test_rejects_an_empty_secret(self) -> None:
        vault = Mock(spec=SecretClient)
        vault.get_secret.return_value = KeyVaultSecret("litellm-master-key", "")
        with self.assertRaisesRegex(RuntimeError, "empty litellm-master-key"):
            await read_gateway_key(vault)


if __name__ == "__main__":
    unittest.main()
