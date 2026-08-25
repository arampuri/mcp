# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 3.0.0

Credential handling moves onto the shared `oracle-mcp-common` library end to end, and
the server gains two new guidance tools.

### Breaking Changes

- **New `oracle-mcp-common` dependency, used for every authentication mode.**
  `session`/`apikey` credentials now come from `oracle_mcp_common.build_auth_context()`,
  and the HTTP transport builds its OAuth provider with
  `oracle_mcp_common.build_idcs_http_auth()` and mints every request's OCI signer with
  that policy's `IDCSHttpAuth.context_for()`. The server-local profile resolution,
  signer construction, and `OCIProvider` wiring are gone, bringing this server onto the
  same authentication path as the other OCI MCP servers. Supported profile
  configurations are unchanged.
- **`ORACLE_MCP_AUTH_METHOD` is no longer needed and is no longer forced.** stdio
  credentials now come from `oracle-mcp-common`'s own resolution, which defaults to
  `auto`: session-token when the selected profile directly declares a
  `security_token_file`, API-key otherwise. 2.x always forced session-token unless the
  variable said `apikey`, so an API-key-only profile failed with "security_token_file
  must be declared directly in the selected OCI_CONFIG_PROFILE" until the variable was
  set. `ORACLE_MCP_AUTH_METHOD` and `ORACLE_MCP_AUTH_PROFILE` remain supported for
  existing configurations, including the unseparated `apikey` spelling; prefer
  `OCI_MCP_AUTH_TYPE` and `OCI_CONFIG_PROFILE`. When both names are set, the `OCI_*`
  one wins.
- **HTTP transport now requires `ORACLE_MCP_BASE_URL`.** Alongside the `IDCS_DOMAIN`,
  `IDCS_CLIENT_ID`, `IDCS_CLIENT_SECRET`, and `IDCS_AUDIENCE` settings 2.1.x already
  required, `oracle-mcp-common` validates all five before the listener starts, so a
  missing or malformed value now fails startup instead of surfacing later as a broken
  sign-in.
- **HTTP transport now requires `ORACLE_MCP_TENANCY_ID`.** Compartment and region
  discovery need a tenancy OCID and there is no local OCI config file to read one from
  on a hosted deployment. `TENANCY_ID_OVERRIDE` is accepted as a synonym.
- **OAuth state moved.** Client registrations and authorization state are now persisted
  by FastMCP under its home directory (`~/.fastmcp/oauth-proxy/`, relocatable with
  `FASTMCP_HOME`), encrypted at rest, with the token-signing key derived from the client
  secret so it is stable across restarts and workers without being configured.
  Deployments that mounted a `.oauth_state` directory must persist the new location
  instead; an ephemeral home directory forces clients to re-register after a restart.
  `FASTMCP_HOME` is resolved when FastMCP is imported, before the server reads its env
  file, so it must be exported rather than set in that file.
- HTTP-mode UPST signers are no longer cached process-wide; a fresh signer is built for
  every tool call from the caller's own request-scoped token.

### Added

- **New `onboard_database_to_recovery_service` guidance tool** for non-destructive Cloud
  Protect onboarding assistance.
- **New `diagnose_recovery_service_issue` guidance tool.** Returns an evidence-driven,
  access-first diagnostic workflow for investigating Oracle Database backup, protection,
  and recoverability problems in a Recovery Service environment.
- Guidance text is now exposed as ordinary tools, so clients without prompt support can
  call it. This brings the tool count to 25.
- `list_protected_databases` now reports retention-lock status and Cloud-Protect-managed
  vs. Database-Service-managed classification.
- `.env` file support (`ORACLE_MCP_ENV_FILE`) so local configuration can live in one file
  instead of exported environment variables.
- OCI requests now carry an `opc-request-id` stamped with opaque installation, caller,
  and tool markers, so a customer-reported call can be traced in service logs without
  identifying the user.

### Changed

- Prompt text moved out of `server.py` into `oracle/oci_recovery_mcp_server/data/prompts/`.
- **CIMD client registration is disabled.** FastMCP enables Client ID Metadata Documents
  by default, which lets a client send an HTTPS URL as its `client_id` and requires this
  server to fetch that URL to learn the client's metadata. That fetch is an outbound
  request made with pinned DNS and redirects disabled, so it fails on a host with no
  egress, and also on one whose egress is a CONNECT proxy. The failure reached the user
  as "The client ID ... was not found in the server's client registry", which reads like
  a client bug. Clients now register with DCR against `/register`, which never leaves the
  host. Startup fails loudly if a future FastMCP release renames the private attribute
  this relies on.
- Updated dependency locks for FastMCP 3.4.5, OCI SDK 2.182.1, and Pydantic 2.13.4.
- README now documents all supported environment variables and the hosted OAuth setup
  inline.

### Fixed

- **Sign-in failed with `invalid_scope` because resource scopes were sent to IDCS
  unqualified.** IDCS names a resource application's scopes by concatenating the
  application's primary audience with the scope name, and `/authorize` accepts only that
  form, so `oci_mcp.recovery.invoke` was rejected and no login could complete. The access
  token IDCS issues carries the scope *bare*, though, and that token is re-validated on
  every request — so qualifying the configured value instead simply moved the failure to
  `401 invalid_token` on the first tool call. `IDCS_REQUIRED_SCOPES` is now bare, as
  verification requires, and the resource scopes advertised to clients are qualified with
  the audience. The two other paths that reach IDCS are qualified as well: the fallback
  used when a client sends no `scope` parameter at all, and the refresh request, which is
  built from the bare scopes stored on the refresh token and would otherwise have killed
  every session at its first refresh an hour after an apparently successful sign-in.
  Startup fails loudly if a future FastMCP release drops the hooks this relies on.
- **Compartment cache could serve one caller's compartments to another.** The compartment
  listing is fetched with `access_level="ACCESSIBLE"`, so it contains exactly what the
  calling identity may see, but it was cached per tenancy only. In a hosted deployment a
  broadly-permissioned user's compartment tree could be served to a restricted one. The
  cache is now keyed by tenancy **and** caller identity; over stdio, where the whole
  process shares one credential, the key is unchanged.
- Tenancy and region lookups now read the same OCI config file **and profile** the
  credentials were resolved from. The OCI SDK only falls back to `OCI_CONFIG_FILE` when
  `~/.oci/config` is absent, and this server resolved `ORACLE_MCP_AUTH_PROFILE` before
  `OCI_CONFIG_PROFILE` while the shared library resolves them in the opposite order, so
  these lookups could resolve a different profile than the request signer.

## 2.1.2

### Changed

- Excluded development artifacts, local configuration, and container build files from source-distribution packages.

## 2.1.1

### Changed

- Updated dependency locks for FastMCP 3.4.5, OCI SDK 2.182.1, and refreshed authentication-related transitive packages.

## 2.1.0

### Added

- Added `list_restore` for retrieving database restore work requests, with filters, paging, and optional child-compartment aggregation.
- Added `check_recovery_service_limits` to report available protected-database backup storage and protected-database-count limits.
- Added `fetch_regions_subscribed` to list the tenancy's subscribed regions and their statuses.

### Changed

- Updated dependency locks for FastMCP 3.4.2, OCI SDK 2.179.0, and refreshed authentication-related transitive packages.
- Added optional child-compartment aggregation to existing compartment-scoped list and summary tools.
- Improved response models with explicit optional-field defaults and descriptions, including the new `WorkRequest` restore-job model.

## 2.0.0

### Breaking Changes

- HTTP transport now requires OCI IAM/IDCS authentication and no longer uses local OCI CLI profile credentials for request authentication.
- HTTP deployments must set `ORACLE_MCP_BASE_URL`, `OCI_REGION`, `IDCS_DOMAIN`, `IDCS_CLIENT_ID`, `IDCS_CLIENT_SECRET`, and `IDCS_AUDIENCE`, and register `${ORACLE_MCP_BASE_URL}/auth/callback`.
- The default required scopes are `openid profile email oci_mcp.recovery.invoke`; set `IDCS_REQUIRED_SCOPES` to override.
