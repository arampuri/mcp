"""
Copyright (c) 2025, Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at
https://oss.oracle.com/licenses/upl.
"""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from oracle_mcp_common import IDCSHttpAuth
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, RequireAuthMiddleware
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials

from oracle.oci_recovery_mcp_server import server
from oracle.oci_recovery_mcp_server.multitenant_auth import TENANT_CLAIM, MultiTenantOCIAuth
from oracle.oci_recovery_mcp_server.tenancy_registry import RegistryError, TenancyRegistry

HOST = "https://mcp.example.com"

_REG = {
    "t1": {
        "tenancy_id": "ocid1.tenancy.oc1..aaaa",
        "idcs_domain": "idcs-aaaa.identity.oraclecloud.com",
        "client_id": "client-one",
        "client_secret": "secret-one",
        "audience": "https://recovery.t1.example.com",
        "region": "us-ashburn-1",
    },
    "t2": {
        "tenancy_id": "ocid1.tenancy.oc1..bbbb",
        "idcs_domain": "idcs-bbbb.identity.oraclecloud.com",
        "client_id": "client-two",
        "client_secret": "secret-two",
        "audience": "https://recovery.t2.example.com",
        "region": "us-phoenix-1",
    },
}


def _fake_oidc(cls, config_url, *, strict=None, timeout_seconds=None):
    host = str(config_url).split("/.well-known")[0]
    return OIDCConfiguration(
        strict=False,
        issuer=host,
        authorization_endpoint=f"{host}/oauth2/v1/authorize",
        token_endpoint=f"{host}/oauth2/v1/token",
        jwks_uri=f"{host}/admin/v1/SigningCert/jwk",
        registration_endpoint=f"{host}/oauth2/v1/register",
        response_types_supported=["code"],
        subject_types_supported=["public"],
        id_token_signing_alg_values_supported=["RS256"],
    )


@pytest.fixture
def auth():
    reg = TenancyRegistry.from_mapping(_REG)
    with patch.object(OIDCConfiguration, "get_oidc_configuration", classmethod(_fake_oidc)):
        yield MultiTenantOCIAuth(
            reg,
            base_url=HOST,
            required_scopes=["openid", "offline_access"],
        )


class TestSharedIDCSHttpAuth:
    """Every tenancy's auth comes from oracle-mcp-common, not from this server."""

    def test_each_tenancy_is_built_by_the_shared_builder(self, auth):
        for alias in ("t1", "t2"):
            policy = auth.http_auth_for(alias)
            assert isinstance(policy, IDCSHttpAuth)
            # exactly one provider per tenancy: the mounted one is the built one
            assert policy.provider is auth._providers[alias]
        assert auth.http_auth_for("unknown") is None
        assert auth.http_auth_for(None) is None

    def test_builder_receives_only_this_tenancy_settings(self):
        # Regression guard for cross-tenancy bleed: the shared builder falls back to
        # process-wide IDCS_* env vars for anything not passed explicitly, so every
        # field must come from the entry and base_url must be the tenancy's mount.
        seen = {}

        def fake_build(scopes, options):
            seen[options.client_id] = (list(scopes), options)
            return SimpleNamespace(provider=object())

        reg = TenancyRegistry.from_mapping(_REG)
        with patch(
            "oracle.oci_recovery_mcp_server.multitenant_auth.build_idcs_http_auth",
            side_effect=fake_build,
        ):
            MultiTenantOCIAuth(reg, base_url=HOST, required_scopes=["openid", "custom.scope"])

        scopes, options = seen["client-two"]
        assert scopes == ["openid", "custom.scope"]
        assert options.domain == "idcs-bbbb.identity.oraclecloud.com"
        assert options.client_secret == "secret-two"
        assert options.audience == "https://recovery.t2.example.com"
        assert options.region == "us-phoenix-1"
        assert options.base_url == f"{HOST}/t/t2"

    def test_policy_carries_the_tenancy_credentials_without_leaking_them(self, auth):
        policy = auth.http_auth_for("t1")
        assert policy._identity_domain_url == "https://idcs-aaaa.identity.oraclecloud.com"
        assert policy._client_id == "client-one"
        assert policy._client_secret == "secret-one"
        assert policy._configured_region == "us-ashburn-1"
        assert "secret-one" not in repr(policy)

    def test_unusable_entry_names_the_tenancy(self):
        bad = {**_REG, "t2": {**_REG["t2"], "idcs_domain": "idcs-bbbb.example.com/oauth2"}}
        reg = TenancyRegistry.from_mapping(bad)
        with patch.object(OIDCConfiguration, "get_oidc_configuration", classmethod(_fake_oidc)):
            with pytest.raises(RegistryError, match=r"\[t2\]"):
                MultiTenantOCIAuth(reg, base_url=HOST)


class TestPerTenancyIsolation:
    """FastMCP derives keys and state storage per tenancy from the client secret."""

    def test_signing_keys_and_storage_differ_per_tenancy(self, auth):
        p1, p2 = auth._providers["t1"], auth._providers["t2"]
        assert p1._jwt_signing_key != p2._jwt_signing_key
        assert p1._client_storage is not p2._client_storage

    def test_signing_key_is_stable_across_restarts(self):
        # Derived from the client secret rather than generated, so a restart (or a
        # second worker) does not invalidate already-issued tokens.
        reg = TenancyRegistry.from_mapping(_REG)
        with patch.object(OIDCConfiguration, "get_oidc_configuration", classmethod(_fake_oidc)):
            first = MultiTenantOCIAuth(reg, base_url=HOST)
            second = MultiTenantOCIAuth(reg, base_url=HOST)
        assert first._providers["t1"]._jwt_signing_key == second._providers["t1"]._jwt_signing_key


class TestRoutes:
    @pytest.mark.asyncio
    async def test_metadata_and_routes_resolve(self, auth):
        app = Starlette(routes=auth.get_routes(mcp_path="/mcp"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            for alias in ("t1", "t2"):
                r = await client.get(f"/.well-known/oauth-authorization-server/t/{alias}")
                assert r.status_code == 200
                assert r.json()["authorization_endpoint"] == f"{HOST}/t/{alias}/authorize"

            # header present -> routes to that tenancy's authorization server
            r = await client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"X-OCI-Tenancy": "t2"},
            )
            assert r.status_code == 200
            assert r.json()["authorization_servers"] == [f"{HOST}/t/t2"]
            assert r.json()["resource"] == f"{HOST}/mcp"

            # OCID also accepted
            r = await client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"X-OCI-Tenancy": "ocid1.tenancy.oc1..aaaa"},
            )
            assert r.json()["authorization_servers"] == [f"{HOST}/t/t1"]

            # header absent -> actionable 400 listing aliases, never secrets
            r = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert r.status_code == 400
            body = r.json()
            assert body["error"] == "tenancy_required"
            assert sorted(body["valid_tenancies"]) == ["t1", "t2"]
            assert "secret-one" not in r.text

    def test_every_tenancy_protects_the_single_mcp_resource(self, auth):
        # The mount /t/<alias> carries only that tenancy's OAuth routes; the resource
        # being protected is the one /mcp endpoint. If a provider derived its resource
        # from its own mount it would reject the resource indicator clients send.
        auth.get_routes(mcp_path="/mcp")
        for provider in auth._providers.values():
            assert str(provider.resource_base_url).rstrip("/") == HOST
            assert str(provider._get_resource_url("/mcp")) == f"{HOST}/mcp"

    @pytest.mark.asyncio
    async def test_operational_routes_mounted(self, auth):
        app = Starlette(routes=auth.get_routes(mcp_path="/mcp"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # empty body -> handler runs (400-ish), but NOT 404 (route resolves)
            r = await client.post("/t/t1/register", json={})
            assert r.status_code != 404
            r = await client.get("/tenancies")
            assert r.status_code == 200 and "t1" in r.text and "t2" in r.text


class TestVerifyTokenStamping:
    @pytest.mark.asyncio
    async def test_stamps_alias_of_matching_provider(self, auth):
        token = AccessToken(token="tok", client_id="c", scopes=[], claims={"jti": "j1"})

        async def t1_load(_tok):
            return None  # t1 doesn't own this token

        async def t2_load(_tok):
            return token  # t2's signing key verifies it

        with patch.object(auth._providers["t1"], "load_access_token", side_effect=t1_load), \
             patch.object(auth._providers["t2"], "load_access_token", side_effect=t2_load):
            result = await auth.verify_token("tok")

        assert result is not None
        assert result.claims[TENANT_CLAIM] == "t2"
        assert result.claims["jti"] == "j1"  # original claims preserved

    @pytest.mark.asyncio
    async def test_returns_none_when_no_provider_matches(self, auth):
        async def none_load(_tok):
            return None

        with patch.object(auth._providers["t1"], "load_access_token", side_effect=none_load), \
             patch.object(auth._providers["t2"], "load_access_token", side_effect=none_load):
            assert await auth.verify_token("tok") is None

    @pytest.mark.asyncio
    async def test_header_narrows_verification_rejecting_mismatched_token(self, auth):
        # A t2 token presented with X-OCI-Tenancy: t1 must be REJECTED (-> 401 ->
        # client re-auths for t1), not silently served as t2.
        token = AccessToken(token="tok", client_id="c", scopes=[], claims={"jti": "j"})

        async def t1_load(_tok):
            return None  # token is not t1's

        async def t2_load(_tok):
            return token  # token is t2's

        with patch("fastmcp.server.dependencies.get_http_headers",
                   return_value={"x-oci-tenancy": "t1"}), \
             patch.object(auth._providers["t1"], "load_access_token", side_effect=t1_load), \
             patch.object(auth._providers["t2"], "load_access_token", side_effect=t2_load):
            # only t1 is consulted because the header asked for t1
            assert await auth.verify_token("tok") is None

    @pytest.mark.asyncio
    async def test_unknown_header_rejected_even_if_token_valid(self, auth):
        # A typo'd / decommissioned X-OCI-Tenancy must be rejected, NOT silently
        # served via try-all, even if a cached token would otherwise verify.
        token = AccessToken(token="tok", client_id="c", scopes=[], claims={"jti": "j"})

        async def valid(_tok):
            return token

        with patch("fastmcp.server.dependencies.get_http_headers",
                   return_value={"x-oci-tenancy": "typo-not-a-tenant"}), \
             patch.object(auth._providers["t1"], "load_access_token", side_effect=valid), \
             patch.object(auth._providers["t2"], "load_access_token", side_effect=valid):
            assert await auth.verify_token("tok") is None

    @pytest.mark.asyncio
    async def test_header_match_verifies(self, auth):
        token = AccessToken(token="tok", client_id="c", scopes=[], claims={"jti": "j"})

        async def t1_load(_tok):
            return token

        with patch("fastmcp.server.dependencies.get_http_headers",
                   return_value={"x-oci-tenancy": "t1"}), \
             patch.object(auth._providers["t1"], "load_access_token", side_effect=t1_load):
            result = await auth.verify_token("tok")
        assert result is not None and result.claims[TENANT_CLAIM] == "t1"


class TestScopeEnforcement:
    """P1 review: oci_mcp.recovery.invoke must be a real, enforced default again,
    not just documented, since a hosted deployment relies on it to keep an
    authenticated-but-unentitled identity out of Recovery tools."""

    def test_default_oauth_scopes_restore_recovery_invoke(self, monkeypatch):
        for name, value in {
            "ORACLE_MCP_AUTH_METHOD": "oauth",
            "ORACLE_MCP_IDCS_DOMAIN": "idcs.example.com",
            "ORACLE_MCP_IDCS_CLIENT_ID": "client-id",
            "ORACLE_MCP_IDCS_CLIENT_SECRET": "client-secret",
            "ORACLE_MCP_IDCS_AUDIENCE": "https://recovery.example.com",
            "ORACLE_MCP_TENANCY_ID": "ocid1.tenancy.oc1..example",
            "ORACLE_MCP_REGION": "us-ashburn-1",
            "ORACLE_MCP_BASE_URL": "https://mcp.example.com",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("ORACLE_MCP_TENANCY_REGISTRY", raising=False)
        monkeypatch.delenv("ORACLE_MCP_OAUTH_SCOPES", raising=False)
        server._reset_registry_cache()

        with patch.object(OIDCConfiguration, "get_oidc_configuration", classmethod(_fake_oidc)):
            provider = server._build_auth_provider()

        assert "oci_mcp.recovery.invoke" in provider.required_scopes
        server._reset_registry_cache()

    @staticmethod
    def _scope_for_token(scopes: list[str]) -> dict:
        token = AccessToken(token="tok", client_id="c", scopes=scopes, claims={})
        return {
            "type": "http",
            "user": AuthenticatedUser(token),
            "auth": AuthCredentials(token.scopes),
        }

    @pytest.mark.asyncio
    async def test_token_missing_recovery_scope_is_rejected(self):
        app_called = False

        async def app(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = RequireAuthMiddleware(app, required_scopes=["openid", "oci_mcp.recovery.invoke"])
        sent = []

        async def send(message):
            sent.append(message)

        await middleware(self._scope_for_token(["openid"]), receive=None, send=send)

        assert app_called is False
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 403

    @pytest.mark.asyncio
    async def test_token_with_recovery_scope_is_allowed(self):
        app_called = False

        async def app(scope, receive, send):
            nonlocal app_called
            app_called = True

        middleware = RequireAuthMiddleware(app, required_scopes=["openid", "oci_mcp.recovery.invoke"])

        async def send(message):
            pass

        await middleware(
            self._scope_for_token(["openid", "oci_mcp.recovery.invoke"]), receive=None, send=send
        )

        assert app_called is True
