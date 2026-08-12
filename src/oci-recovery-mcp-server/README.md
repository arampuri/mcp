# OCI Recovery Service MCP Server

An Oracle Cloud Infrastructure (OCI) Model Context Protocol (MCP) server for read-oriented Autonomous Recovery Service and Database Service operations. It maps OCI SDK responses to Pydantic models suitable for MCP clients.

## What it provides

- Browse protected databases, protection policies, Recovery Service subnets, restore work requests, DB homes, DB systems, databases, and backups.
- Summarize protected-database health, redo-shipping status, backup-space consumption, and backup destinations.
- Query Recovery Service metrics, service limits, and tenancy region subscriptions.
- Aggregate supported list and summary operations across a compartment subtree.
- Provide non-destructive Recovery Service dashboard and Cloud Protect onboarding guidance.
- Run locally with OCI API-key or session-token authentication, or as a hosted multi-tenant OAuth service.

The server exposes 24 MCP tools. It does not create, update, or delete OCI resources.

## Requirements

- Python 3.13 or later
- [`uv`](https://docs.astral.sh/uv/)
- OCI credentials appropriate to the selected authentication mode

## Install and run locally

From the repository root:

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e .
```

Choose an authentication method. Configuration comes from environment variables, which
may also be placed in a `.env` file next to the server.

For a session-token profile, authenticate once and set the corresponding profile:

```dotenv
ORACLE_MCP_AUTH_METHOD=session
ORACLE_MCP_AUTH_PROFILE=DEFAULT
```

```sh
oci session authenticate --profile-name DEFAULT
```

For API-key authentication, use `ORACLE_MCP_AUTH_METHOD=apikey`; the selected OCI profile must contain the normal API-key fields. The server defaults to stdio when `ORACLE_MCP_HOST` and `ORACLE_MCP_PORT` are unset:

```sh
.venv/bin/oracle.oci-recovery-mcp-server
```

The server loads `.env` from the working directory or a parent directory. Set `ORACLE_MCP_ENV_FILE` to use a specific configuration file; explicitly exported variables take precedence.

### Local MCP client configuration

Configure an MCP client to start the installed entry point. For example:

```json
{
  "mcpServers": {
    "oci-recovery-local": {
      "type": "stdio",
      "command": "/ABS/PATH/oci-recovery-mcp-server/.venv/bin/oracle.oci-recovery-mcp-server",
      "cwd": "/ABS/PATH/oci-recovery-mcp-server",
      "env": {
        "ORACLE_MCP_AUTH_METHOD": "session",
        "ORACLE_MCP_AUTH_PROFILE": "DEFAULT"
      }
    }
  }
}
```

To run a local HTTP listener for `session` or `apikey` mode, set both values below. This listener uses the server's local OCI credentials, so expose it only on a trusted network.

```dotenv
ORACLE_MCP_HOST=127.0.0.1
ORACLE_MCP_PORT=7337
```

## Hosted multi-tenant OAuth

OAuth mode runs one Streamable HTTP server for one or more tenancies. Each client sends `X-OCI-Tenancy` with the configured tenancy alias (or tenancy OCID) to select the correct sign-in and OCI request routing context. The tenancy registry, OAuth client secrets, signing keys, and OAuth state remain server-side.

Set `ORACLE_MCP_AUTH_METHOD=oauth` and configure `ORACLE_MCP_TENANCY_REGISTRY`. Each registry entry supplies a tenancy OCID, IAM domain, confidential-client credentials, and OCI region. The supported single-tenant fallback is the legacy `ORACLE_MCP_IDCS_DOMAIN`, `ORACLE_MCP_IDCS_CLIENT_ID`, `ORACLE_MCP_IDCS_CLIENT_SECRET`, `ORACLE_MCP_TENANCY_ID`, and `ORACLE_MCP_REGION` variables.

`ORACLE_MCP_BASE_URL` is required and must be an absolute `https://` URL: this is what per-tenancy authorize/callback/well-known URLs are built from, so a missing or plain-HTTP value fails startup rather than silently advertising `http://localhost:8000`. Set `ORACLE_MCP_OAUTH_ALLOW_INSECURE_LOCAL=true` to allow an `http://localhost` base URL for local development only.

The default `ORACLE_MCP_OAUTH_SCOPES` includes `oci_mcp.recovery.invoke`, which gates access to this server's Recovery tools beyond a bare authenticated identity. If you override `ORACLE_MCP_OAUTH_SCOPES`, keep `oci_mcp.recovery.invoke` in the list.

The registry is a TOML file with one table per tenancy. The table name is the URL-safe
tenancy name (letters, digits, `-`, `_`) used both in the `X-OCI-Tenancy` header and in
the per-tenancy OAuth callback path `<base_url>/t/TENANCY_NAME/auth/callback`, which must
be registered as a redirect URI on the corresponding IAM confidential application.

```toml
[TENANCY_NAME]
tenancy_id    = "ocid1.tenancy.oc1..aaaa"
idcs_domain   = "idcs-aaaa.identity.oraclecloud.com"   # host or full https URL
client_id     = "REPLACE_ME"
client_secret = "REPLACE_ME"
region        = "us-ashburn-1"
# Optional: pin the token-signing key. If omitted, one is generated and
# persisted per tenancy under ORACLE_MCP_OAUTH_STORAGE_DIR.
# jwt_signing_key = "generate with: openssl rand -hex 32"
```

This file holds OAuth client secrets and must never be committed or served. Restrict it
to the service user (mode `640`). Per-tenancy OAuth state and signing keys are written
under `ORACLE_MCP_OAUTH_STORAGE_DIR` (default `.oauth_state` in the working directory);
treat that directory as secret material and exclude it from images and version control.

Run the HTTP listener behind a TLS-terminating reverse proxy and bind it to
`127.0.0.1`. `ORACLE_MCP_BASE_URL` must be the public `https://` URL clients reach.

After deployment, a remote client configuration has this form:

```json
{
  "mcpServers": {
    "oci-recovery": {
      "type": "streamableHttp",
      "url": "https://MCP_HOST/mcp",
      "headers": {
        "X-OCI-Tenancy": "TENANCY_NAME"
      }
    }
  }
}
```

For a VPN-only deployment whose proxy uses an internal CA, clients must trust that CA's
public root certificate. Distribute only the public root certificate, never the private key.

## Environment variables

| Variable | Modes | Description |
| --- | --- | --- |
| `ORACLE_MCP_AUTH_METHOD` | all | `session`, `apikey`, or `oauth`. Defaults to `session`. |
| `ORACLE_MCP_AUTH_PROFILE` | session, apikey | Profile in `~/.oci/config`. Falls back to `OCI_CONFIG_PROFILE`, then `DEFAULT`. |
| `ORACLE_MCP_ENV_FILE` | all | Path to a specific `.env` file instead of directory discovery. |
| `ORACLE_MCP_HOST`, `ORACLE_MCP_PORT` | all | Bind address for the HTTP listener. Both must be set. Plain HTTP is rejected for `session`/`apikey` unless the listener is local-only. |
| `ORACLE_MCP_TENANCY_REGISTRY` | oauth | Path to the server-side tenancy registry TOML. |
| `ORACLE_MCP_BASE_URL` | oauth | Required. Absolute `https://` public URL used to build authorize, callback, and well-known URLs. |
| `ORACLE_MCP_OAUTH_ALLOW_INSECURE_LOCAL` | oauth | Allows an unset or `http://localhost` base URL. Local development only. |
| `ORACLE_MCP_OAUTH_STORAGE_DIR` | oauth | Directory for per-tenancy OAuth state and signing keys. Contains secret material. |
| `ORACLE_MCP_OAUTH_SCOPES` | oauth | Requested scopes. Default includes `oci_mcp.recovery.invoke` and `offline_access`. |
| `ORACLE_MCP_OAUTH_REQUIRE_CONSENT` | oauth | Keep `true` on shared deployments. |
| `ORACLE_MCP_OAUTH_REDIRECT_PATH` | oauth | Callback path. Default `/auth/callback`. |
| `ORACLE_MCP_TENANCY_ALIAS`, `ORACLE_MCP_IDCS_DOMAIN`, `ORACLE_MCP_IDCS_CLIENT_ID`, `ORACLE_MCP_IDCS_CLIENT_SECRET`, `ORACLE_MCP_TENANCY_ID`, `ORACLE_MCP_REGION`, `ORACLE_MCP_JWT_SIGNING_KEY` | oauth | Single-tenant fallback used when `ORACLE_MCP_TENANCY_REGISTRY` is unset. |
| `ORACLE_MCP_INSTALLATION_ID`, `ORACLE_MCP_INSTALLATION_ID_FILE` | all | Stable installation identifier for telemetry. Set explicitly on shared deployments. |
| `ORACLE_MCP_LOG_LEVEL`, `ORACLE_MCP_LOG_TO_STDOUT`, `ORACLE_MCP_LOG_DIR`, `ORACLE_MCP_LOG_FILE`, `ORACLE_SDK_LOG_LEVEL` | all | Logging configuration. |

## Tools

Most resource tools accept `region` where the OCI API supports it. The supported list and summary tools accept `fetch_for_child_compartment=true` to include the requested compartment and its descendants.

| Area | Tools |
| --- | --- |
| Protected databases | `list_protected_databases`, `get_protected_database`, `summarize_protected_database_health`, `summarize_protected_database_redo_status`, `summarize_backup_space_used` |
| Recovery Service | `check_recovery_service_limits`, `fetch_regions_subscribed`, `list_protection_policies`, `get_protection_policy`, `list_recovery_service_subnets`, `get_recovery_service_subnet`, `get_recovery_service_metrics`, `list_restore` |
| Database Service and backups | `list_databases`, `get_database`, `list_backups`, `get_backup`, `summarize_protected_database_backup_destination`, `list_db_homes`, `get_db_home`, `list_db_systems`, `get_db_system` |
| Guidance | `oci_recovery_service_dashboard_prompt`, `onboard_database_to_recovery_service` |

Use the MCP tool descriptions as the authoritative parameter reference. Some list tools can resolve a compartment display name; OCI resource retrieval tools require the corresponding OCID.

## Development and validation

Install the development dependencies before running tests:

```sh
uv sync --group dev
uv run pytest
```

The test suite is offline: OCI clients are mocked, so no credentials or live resources
are required.

## License

Copyright (c) 2025, 2026 Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at <https://oss.oracle.com/licenses/upl>.
