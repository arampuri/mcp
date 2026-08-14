"""
Copyright (c) 2025, Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at
https://oss.oracle.com/licenses/upl.
"""

# Multi-tenant OCI IAM (IDCS) OAuth provider for the single-hosted deployment.
#
# One process serves many tenancies behind a single MCP URL (https://host/mcp).
# A user selects their tenancy with the `X-OCI-Tenancy` HTTP header (alias or OCID).
#
# How it works
# ------------
# FastMCP binds ONE auth provider per server and mounts its routes once. We build
# one self-contained `OCIProvider` per tenancy (each is an OIDC proxy to that
# tenancy's IDCS) and compose them under one app:
#
#   * Operational OAuth routes for each tenancy are mounted under /t/<alias>/...
#     (/authorize, /token, /register, /auth/callback, /consent), so the upstream
#     redirect URL is https://host/t/<alias>/auth/callback.
#   * Each tenancy's authorization-server metadata is served (path-aware, per
#     RFC 8414) at /.well-known/oauth-authorization-server/t/<alias>.
#   * A single protected-resource metadata endpoint (/.well-known/oauth-protected-
#     resource/mcp) reads the X-OCI-Tenancy header and points the client at that
#     tenancy's authorization server, so the browser login auto-routes. If the
#     header is missing/unknown it returns an actionable 400 listing valid aliases.
#   * Token verification tries each tenancy's verifier; the one whose signing key
#     matches wins (verification never trusts the header). The verified token is
#     stamped with the `oracle_mcp_tenant_alias` claim so tool routing is bound to
#     the proven identity, not a mutable request header.
#
# Authentication itself is not implemented here. Each tenancy's provider and
# request-scoped OCI credentials come from oracle-mcp-common's
# build_idcs_http_auth() / IDCSHttpAuth.context_for(); this module only decides
# which tenancy a request belongs to and composes the per-tenancy routes.
#
# Per-tenancy isolation (matching the old one-process-per-tenancy deployment) is
# preserved by the shared builder rather than configured here: FastMCP derives both
# the token signing key and the encrypted OAuth-state directory from the upstream
# client secret, which differs per tenancy, so no tenancy can read another's state
# and keys stay stable across restarts and workers.

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from oracle_mcp_common import IDCSHttpAuth, IDCSHttpAuthOptions, build_idcs_http_auth
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from fastmcp.server.auth.auth import AccessToken, AuthProvider
from fastmcp.utilities.logging import get_logger

from .tenancy_registry import RegistryError, TenancyEntry, TenancyRegistry

logger = get_logger(__name__)

TENANT_CLAIM = "oracle_mcp_tenant_alias"


class MultiTenantOCIAuth(AuthProvider):
    """Compose one OCIProvider per tenancy behind a single MCP URL, header-routed."""

    def __init__(
        self,
        registry: TenancyRegistry,
        *,
        base_url: str,
        required_scopes: Optional[list[str]] = None,
    ):
        super().__init__(base_url=base_url, required_scopes=required_scopes or ["openid"])
        self._registry = registry
        self._root = str(self.base_url).rstrip("/")
        self._real_resource: Optional[AnyHttpUrl] = None

        self._http_auth = {e.alias: self._build_http_auth(e) for e in registry.entries}
        self._providers = {alias: h.provider for alias, h in self._http_auth.items()}
        logger.info(
            "Multi-tenant OCI OAuth initialized for %d tenancies: %s",
            len(self._providers),
            ", ".join(sorted(self._providers)),
        )

    # -- construction ---------------------------------------------------------

    def _build_http_auth(self, entry: TenancyEntry) -> IDCSHttpAuth:
        """Build one tenancy's shared IDCS HTTP auth via oracle-mcp-common.

        Every authentication input is passed explicitly per tenancy, so the shared
        builder never falls back to a process-wide IDCS_* environment variable and
        one tenancy's domain, client, or audience can never be applied to another.
        `base_url` is this tenancy's own mount, which is what makes the resulting
        provider advertise (and accept) /t/<alias>/authorize and
        /t/<alias>/auth/callback.

        Nothing about the provider is configured locally: the token signing key and
        the encrypted OAuth-state directory are derived by FastMCP from this
        tenancy's client secret, and consent and the /auth/callback redirect path
        take the shared library's defaults.
        """
        alias = entry.alias
        try:
            return build_idcs_http_auth(
                list(self.required_scopes),
                IDCSHttpAuthOptions(
                    domain=entry.idcs_domain,
                    client_id=entry.client_id,
                    client_secret=entry.client_secret,
                    audience=entry.audience,
                    base_url=f"{self._root}/t/{alias}",
                    region=entry.region,
                ),
            )
        except ValueError as e:
            # Name the tenancy: with many entries the shared message alone doesn't
            # say which registry table is at fault.
            raise RegistryError(f"Registry entry [{alias}] cannot be used for OAuth: {e}") from e

    def http_auth_for(self, alias: Optional[str]) -> Optional[IDCSHttpAuth]:
        """Return a tenancy's shared IDCS HTTP auth policy (None if unknown).

        Callers exchange the current request's access token through
        IDCSHttpAuth.context_for(); the returned signer is request-scoped and must
        never be cached. Only the policy itself -- provider plus this tenancy's own
        server-side credentials -- is long-lived, exactly like the provider it wraps.
        """
        return self._http_auth.get(alias) if alias else None

    # -- token verification (token-authoritative tenant binding) --------------

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Verify the bearer token and stamp the proven tenant alias as a claim.

        The X-OCI-Tenancy header drives which tenancy may verify the token:
          * known tenancy  -> only that provider may verify (a token for a
            *different* tenancy then fails -> 401 -> client re-authenticates for
            the requested tenancy, instead of being silently served the old one);
          * present but unknown (e.g. a typo or a decommissioned alias) -> reject
            (-> 401), mirroring the `tenancy_required` behavior at OAuth discovery,
            so a stale cached token can't quietly authenticate under the wrong name;
          * absent -> fall back to trying every provider (the token itself is proof
            of a prior valid login).
        Security is unchanged in all cases: a token is only ever accepted for the
        tenancy whose signing key actually verifies it.
        """
        candidates = list(self._providers.items())

        try:
            from fastmcp.server.dependencies import get_http_headers

            hint = (get_http_headers() or {}).get("x-oci-tenancy")
        except Exception:
            hint = None
        hint = hint.strip() if hint else ""

        if hint:
            hinted = self._registry.lookup(hint)
            if hinted is None or hinted.alias not in self._providers:
                logger.info(
                    "Bearer auth rejected: X-OCI-Tenancy header names an unknown tenancy."
                )
                return None
            candidates = [(hinted.alias, self._providers[hinted.alias])]

        for alias, provider in candidates:
            try:
                validated = await provider.load_access_token(token)
            except Exception:
                validated = None
            if validated is not None:
                claims = {**(validated.claims or {}), TENANT_CLAIM: alias}
                return validated.model_copy(update={"claims": claims})
        return None

    # -- routes ---------------------------------------------------------------

    def get_routes(self, mcp_path: Optional[str] = None) -> list:
        self._real_resource = self._get_resource_url(mcp_path)
        routes: list = []

        for alias, provider in self._providers.items():
            # Every tenancy protects the same single resource, the /mcp endpoint --
            # not /t/<alias>/mcp, which is only where that tenancy's OAuth routes are
            # mounted. Declaring it before get_routes() lets the provider derive its
            # own resource URL (and the audience of the tokens it issues) from it,
            # so the resource indicator a client sends is the one it validates.
            provider.resource_base_url = self.base_url

            all_routes = provider.get_routes(mcp_path=mcp_path)

            # Path-aware authorization-server metadata stays at the root level.
            for wk in provider.get_well_known_routes(mcp_path=mcp_path):
                if isinstance(wk, Route) and "oauth-authorization-server" in wk.path:
                    routes.append(wk)

            # Operational routes (/authorize, /token, /register, /auth/callback,
            # /consent) get mounted under /t/<alias>/... so their advertised URLs
            # resolve. (get_routes also emits a per-provider protected-resource
            # route pointing at /t/<alias>/mcp which we intentionally drop in
            # favor of the single header-aware endpoint below.)
            op = [
                r
                for r in all_routes
                if isinstance(r, Route) and not r.path.startswith("/.well-known/")
            ]
            routes.append(Mount(f"/t/{alias}", routes=op))

        # Single, header-aware protected-resource metadata (RFC 9728).
        pr_path = urlparse(str(build_resource_metadata_url(self._real_resource))).path
        routes.append(
            Route(
                pr_path,
                endpoint=cors_middleware(self._protected_resource_metadata, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            )
        )

        # Human-facing helper page (lists tenancy aliases; no secrets).
        routes.append(Route("/tenancies", endpoint=self._tenancies_page, methods=["GET"]))
        return routes

    # -- handlers -------------------------------------------------------------

    async def _protected_resource_metadata(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204)

        hint = request.headers.get("x-oci-tenancy")
        entry = self._registry.lookup(hint)
        if entry is None:
            # No usable tenancy: tell the client exactly what to set (aliases only).
            logger.info(
                "protected-resource discovery without a valid X-OCI-Tenancy header "
                "(value present=%s); returning tenancy_required",
                bool(hint),
            )
            return JSONResponse(
                {
                    "error": "tenancy_required",
                    "error_description": (
                        "Set the 'X-OCI-Tenancy' header (tenancy OCID or alias) to one of: "
                        + ", ".join(sorted(self._registry.aliases))
                    ),
                    "valid_tenancies": sorted(self._registry.aliases),
                },
                status_code=400,
            )

        return JSONResponse(
            {
                "resource": str(self._real_resource),
                "authorization_servers": [f"{self._root}/t/{entry.alias}"],
                "scopes_supported": list(self.required_scopes),
                "bearer_methods_supported": ["header"],
            }
        )

    async def _tenancies_page(self, request: Request) -> HTMLResponse:
        rows = "".join(f"<li><code>{a}</code></li>" for a in sorted(self._registry.aliases))
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>OCI Recovery MCP - tenancies</title></head><body>"
            "<h2>OCI Recovery MCP server</h2>"
            "<p>This is a multi-tenancy MCP server. In your MCP client config, set the "
            "<code>X-OCI-Tenancy</code> header to your tenancy OCID or one of these aliases:</p>"
            f"<ul>{rows}</ul>"
            "</body></html>"
        )
        return HTMLResponse(html)
