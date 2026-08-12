# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 3.0.0

### Breaking Changes

- **Hosted OAuth requires explicit configuration.** `ORACLE_MCP_AUTH_METHOD=oauth`
  now requires either `ORACLE_MCP_TENANCY_REGISTRY` (a `tenancies.toml` file) or
  the legacy single-tenant `ORACLE_MCP_IDCS_*`/`ORACLE_MCP_TENANCY_ID`/`ORACLE_MCP_REGION`
  env vars, **and** an absolute `https://` `ORACLE_MCP_BASE_URL`. A missing or
  plain-HTTP base URL now fails startup instead of silently advertising
  `http://localhost:8000` authorization/callback URLs. Set
  `ORACLE_MCP_OAUTH_ALLOW_INSECURE_LOCAL=true` to keep using an
  `http://localhost` base URL for local development.
- **Default OAuth scopes include `oci_mcp.recovery.invoke` again.** Deployments
  that pin `ORACLE_MCP_OAUTH_SCOPES` explicitly must add `oci_mcp.recovery.invoke`
  back to that list themselves, or authenticated-but-unentitled identities will
  regain access to Recovery tools.
- **`ORACLE_MCP_HOST`/`ORACLE_MCP_PORT` (plain HTTP) no longer work with
  `session`/`apikey` auth.** Those auth methods carry the operator's own OCI
  credentials and have no per-caller authentication story, so they now only run
  over stdio; setting host/port without `ORACLE_MCP_AUTH_METHOD=oauth` is a
  startup error. Use `oauth` mode for any HTTP-reachable deployment.
- **Per-tenant OAuth callback route** moved to `/t/<alias>/auth/callback`
  (previously a single `/auth/callback`), to support the multi-tenant hosted
  model. Update any registered IAM confidential-application redirect URIs.
- **New `oracle-mcp-common` dependency.** `apikey`/`session` credential
  resolution now goes through `oracle_mcp_common.build_auth_context()` instead
  of server-local profile/signer code; no functional change to supported
  profile configurations.
- OAuth-mode UPST signers are no longer cached process-wide; a fresh signer is
  built for every tool call from the caller's own request-scoped token.

### Added

- Multi-tenant OAuth (`ORACLE_MCP_AUTH_METHOD=oauth`): one hosted server can now
  serve multiple tenancies behind a single MCP URL, selected via the
  `X-OCI-Tenancy` header.
- `onboard_database_to_recovery_service` guidance tool for non-destructive Cloud
  Protect onboarding assistance, bringing the tool count to 24.
- `list_protected_databases` now reports retention-lock status and
  Cloud-Protect-managed vs. Database-Service-managed classification.
- `.env` file support (`ORACLE_MCP_ENV_FILE`) so local configuration can live in
  one file instead of exported environment variables.

### Changed

- Updated dependency locks for FastMCP 3.4.5, OCI SDK 2.182.1, and Pydantic
  2.13.4.
- README now documents all supported environment variables and the tenancy
  registry format inline.

### Fixed

- Tenancy and region lookups now read the same OCI config file the credentials
  were resolved from. The OCI SDK only falls back to `OCI_CONFIG_FILE` when
  `~/.oci/config` is absent, so with both present these lookups could resolve a
  different profile than the request signer.

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