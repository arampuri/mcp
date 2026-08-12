"""
Copyright (c) 2025, Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at
https://oss.oracle.com/licenses/upl.
"""

import os
import stat
from unittest.mock import patch

import httpx
import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, RequireAuthMiddleware
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials

from oracle.oci_recovery_mcp_server import server
from oracle.oci_recovery_mcp_server.multitenant_auth import (
    TENANT_CLAIM,
    MultiTenantOCIAuth,
    load_or_create_signing_key,
)
from oracle.oci_recovery_mcp_server.tenancy_registry import TenancyRegistry

HOST = "https://mcp.example.com"

_REG = {
    "t1": {
        "tenancy_id": "ocid1.tenancy.oc1..aaaa",
        "idcs_domain": "idcs-aaaa.identity.oraclecloud.com",
        "client_id": "client-one",
        "client_secret": "secret-one",
        "region": "us-ashburn-1",
    },
    "t2": {
        "tenancy_id": "ocid1.tenancy.oc1..bbbb",
        "idcs_domain": "idcs-bbbb.identity.oraclecloud.com",
        "client_id": "client-two",
        "client_secret": "secret-two",
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
def auth(tmp_path):
    reg = TenancyRegistry.from_mapping(_REG)
    with patch.object(OIDCConfiguration, "get_oidc_configuration", classmethod(_fake_oidc)):
        yield MultiTenantOCIAuth(
            reg,
            base_url=HOST,
            storage_root=str(tmp_path),
            required_scopes=["openid", "offline_access"],
        )


class TestSigningKey:
    def test_persisted_once_with_0600(self, tmp_path):
        k1 = load_or_create_signing_key(str(tmp_path), "t1")
        k2 = load_or_create_signing_key(str(tmp_path), "t1")
        assert k1 == k2 and len(k1) == 32  # stable, never regenerated
        key_path = tmp_path / "t1" / "signing.key"
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        assert mode == 0o600


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
