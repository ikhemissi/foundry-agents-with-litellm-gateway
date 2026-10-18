import logging
import os
import sys
from pathlib import Path

import uvicorn

def configure_audit_log() -> None:
    """Route the nonsecret decision log to the container's stderr, independent of LiteLLM's levels."""
    audit = logging.getLogger("gateway.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    if not audit.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("gateway.audit %(message)s"))
        audit.addHandler(handler)


def main() -> None:
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["LITELLM_TELEMETRY"] = "False"
    os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"] = (
        "authorization,api-key,x-client-.*,.*token.*,.*secret.*"
    )
    from entra_auth import verifier
    from gateway_app import CredentialBoundary

    configure_audit_log()

    mode = os.environ.get("LITELLM_AUTH_MODE")
    if mode not in ("api_key", "entra"):
        raise ValueError("LITELLM_AUTH_MODE must be api_key or entra.")
    if mode == "entra":
        verifier()  # Validate nonsecret policy configuration before accepting requests.
    config = "config.entra.yaml" if mode == "entra" else "config.yaml"
    os.environ["CONFIG_FILE_PATH"] = str(Path(__file__).with_name(config))
    from litellm.proxy.proxy_server import app

    uvicorn.run(CredentialBoundary(app, mode), host="0.0.0.0", port=4000, access_log=False)


if __name__ == "__main__":
    main()
