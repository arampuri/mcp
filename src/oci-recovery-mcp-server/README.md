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

For API-key authentication, use `ORACLE_MCP_AUTH_METHOD=apikey`; the selected OCI profile must contain the normal API-key fields. The `session` and `apikey` methods run over stdio only:

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

`session` and `apikey` mode cannot be served over a network listener: they carry the
operator's own OCI credentials and have no per-caller authentication. Setting
`ORACLE_MCP_HOST` and `ORACLE_MCP_PORT` in these modes fails startup; both variables are
reserved for `oauth` mode. To expose this server over HTTP, use the hosted OAuth
deployment below.

## Hosted multi-tenant OAuth

OAuth mode runs one Streamable HTTP server for one or more tenancies. Each client sends `X-OCI-Tenancy` with the configured tenancy alias (or tenancy OCID) to select the correct sign-in and OCI request routing context. The tenancy registry, OAuth client secrets, signing keys, and OAuth state remain server-side.

Set `ORACLE_MCP_AUTH_METHOD=oauth` and configure `ORACLE_MCP_TENANCY_REGISTRY`. Each registry entry supplies a tenancy OCID, IAM domain, confidential-client credentials, resource audience, and OCI region. The supported single-tenant fallback is the legacy `ORACLE_MCP_IDCS_DOMAIN`, `ORACLE_MCP_IDCS_CLIENT_ID`, `ORACLE_MCP_IDCS_CLIENT_SECRET`, `ORACLE_MCP_IDCS_AUDIENCE`, `ORACLE_MCP_TENANCY_ID`, and `ORACLE_MCP_REGION` variables.

When registering the IAM domain integrated application for a tenancy, configure a
**primary audience** on its resource application and put that exact value in the
entry's `audience`. It is both the `aud` claim issued access tokens are verified
against and the audience requested at `/authorize` and `/token`, so a value that does
not match the domain's configuration makes every sign-in for that tenancy fail.

`ORACLE_MCP_BASE_URL` is required and must be an absolute `https://` URL: this is what per-tenancy authorize/callback/well-known URLs are built from, so a missing or plain-HTTP value fails startup rather than silently advertising `http://localhost:8000`. Set `ORACLE_MCP_OAUTH_ALLOW_INSECURE_LOCAL=true` to allow an `http://localhost` base URL for local development only.

The default `ORACLE_MCP_OAUTH_SCOPES` includes `oci_mcp.recovery.invoke`, which gates access to this server's Recovery tools beyond a bare authenticated identity. If you override `ORACLE_MCP_OAUTH_SCOPES`, keep `oci_mcp.recovery.invoke` in the list.

Write resource scopes **bare**, never qualified with the audience. IAM names a resource
application's scopes by concatenating its primary audience with the scope name, and
`/authorize` accepts only that form — but the access token it issues carries the scope
bare, and that token is re-validated on every request against this same setting. The
server reconciles the two: it verifies against the configured value and qualifies each
tenancy's resource scopes with that tenancy's own audience before advertising them to
clients. Qualifying them yourself produces `401 invalid_token` on the first tool call,
after a sign-in that appeared to succeed.

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
audience      = "REPLACE_ME"                           # resource app primary audience
region        = "us-ashburn-1"
```

This file holds OAuth client secrets and must never be committed or served. Restrict it
to the service user (mode `640`).

Per-tenancy OAuth state (client registrations and authorization state) is persisted and
encrypted at rest by FastMCP under its home directory, `~/.fastmcp/oauth-proxy/` by
default and relocatable with `FASTMCP_HOME`. The storage directory and the token-signing
key are both derived from each tenancy's `client_secret`, so tenancies stay isolated from
one another and keys survive restarts and multiple workers without being stored in the
registry. Treat that directory as secret material, exclude it from images, and give it
persistent storage in a container deployment — a fresh directory on every restart forces
all clients to re-register. Rotating a tenancy's `client_secret` invalidates its
already-issued tokens, and its clients sign in again.

The first authorization for a tenancy shows a consent screen; subsequent tool calls reuse
the granted session.

Clients register through Dynamic Client Registration at `/t/TENANCY_NAME/register`, an
exchange that never leaves the host, so authentication needs no outbound internet access.
CIMD (Client ID Metadata Document) registration is deliberately disabled: it would let a
client present an HTTPS URL as its `client_id` and require this server to fetch that URL,
which fails on a network-restricted host and surfaces as `The client ID ... was not found
in the server's client registry`.

OAuth discovery cannot carry `X-OCI-Tenancy` — a client fetches the well-known metadata
before it has an MCP session, and that header rides only on requests to the MCP URL. When
exactly one tenancy is configured there is nothing to disambiguate, so discovery is
answered for it. A multi-tenancy registry still requires the header, and therefore a
client able to send it.

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
| `ORACLE_MCP_HOST`, `ORACLE_MCP_PORT` | oauth | Bind address for the Streamable HTTP listener. Defaults to `127.0.0.1:8000`. Rejected in `session`/`apikey` mode, which run over stdio only. |
| `ORACLE_MCP_TENANCY_REGISTRY` | oauth | Path to the server-side tenancy registry TOML. |
| `ORACLE_MCP_BASE_URL` | oauth | Required. Absolute `https://` public URL used to build authorize, callback, and well-known URLs. |
| `ORACLE_MCP_OAUTH_ALLOW_INSECURE_LOCAL` | oauth | Allows an unset or `http://localhost` base URL. Local development only. |
| `ORACLE_MCP_OAUTH_SCOPES` | oauth | Requested scopes. Default includes `oci_mcp.recovery.invoke` and `offline_access`. |
| `FASTMCP_HOME` | oauth | FastMCP's home directory, where per-tenancy OAuth state is persisted. Contains secret material. |
| `ORACLE_MCP_TENANCY_ALIAS`, `ORACLE_MCP_IDCS_DOMAIN`, `ORACLE_MCP_IDCS_CLIENT_ID`, `ORACLE_MCP_IDCS_CLIENT_SECRET`, `ORACLE_MCP_IDCS_AUDIENCE`, `ORACLE_MCP_TENANCY_ID`, `ORACLE_MCP_REGION` | oauth | Single-tenant fallback used when `ORACLE_MCP_TENANCY_REGISTRY` is unset. |
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
