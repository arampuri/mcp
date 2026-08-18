"""
Copyright (c) 2025, Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at
https://oss.oracle.com/licenses/upl.
"""

import textwrap
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from oracle.oci_recovery_mcp_server.tenancy_registry import (
    RegistryError,
    TenancyRegistry,
    load_registry,
)
from oracle.oci_recovery_mcp_server import server
from oracle.oci_recovery_mcp_server.multitenant_auth import TENANT_CLAIM


def _write(tmp_path, body: str):
    p = tmp_path / "tenancies.toml"
    p.write_text(textwrap.dedent(body))
    return str(p)


_GOOD = """
    [t1]
    tenancy_id    = "ocid1.tenancy.oc1..aaaa"
    idcs_domain   = "idcs-aaaa.identity.oraclecloud.com"
    client_id     = "client-one"
    client_secret = "secret-one"
    audience      = "https://recovery.t1.example.com"
    region        = "us-ashburn-1"

    [t2]
    tenancy_id    = "ocid1.tenancy.oc1..bbbb"
    idcs_domain   = "idcs-bbbb.identity.oraclecloud.com"
    client_id     = "client-two"
    client_secret = "secret-two"
    audience      = "https://recovery.t2.example.com"
    region        = "us-phoenix-1"
"""


class TestTenancyRegistry:
    def test_loads_and_indexes_by_alias_and_ocid(self, tmp_path):
        reg = TenancyRegistry.from_file(_write(tmp_path, _GOOD))
        assert len(reg) == 2
        assert sorted(reg.aliases) == ["t1", "t2"]

        by_alias = reg.lookup("t1")
        assert by_alias.tenancy_id == "ocid1.tenancy.oc1..aaaa"
        assert by_alias.region == "us-ashburn-1"

        by_ocid = reg.lookup("ocid1.tenancy.oc1..bbbb")
        assert by_ocid.alias == "t2"
        assert by_ocid.audience == "https://recovery.t2.example.com"

        assert reg.lookup("nope") is None
        assert reg.lookup(None) is None
        assert reg.lookup("  t1  ") is not None  # trims whitespace

    def test_repr_does_not_leak_secret(self, tmp_path):
        reg = TenancyRegistry.from_file(_write(tmp_path, _GOOD))
        entry = reg.lookup("t1")
        text = repr(entry)
        # secrets must never appear in the repr (used in logs)
        assert entry.client_secret not in text
        assert entry.client_id not in text
        assert "t1" in text

    def test_missing_required_field_raises(self, tmp_path):
        body = """
            [t1]
            tenancy_id  = "ocid1.tenancy.oc1..aaaa"
            idcs_domain = "idcs-aaaa.identity.oraclecloud.com"
            client_id   = "cid1"
            audience    = "https://recovery.t1.example.com"
            region      = "us-ashburn-1"
        """
        with pytest.raises(RegistryError, match="client_secret"):
            TenancyRegistry.from_file(_write(tmp_path, body))

    def test_missing_audience_raises(self, tmp_path):
        # The IAM resource audience is per tenancy and is what issued tokens are
        # verified against, so it must never be defaulted or inherited.
        body = """
            [t1]
            tenancy_id    = "ocid1.tenancy.oc1..aaaa"
            idcs_domain   = "idcs-aaaa.identity.oraclecloud.com"
            client_id     = "cid1"
            client_secret = "sec1"
            region        = "us-ashburn-1"
        """
        with pytest.raises(RegistryError, match="audience"):
            TenancyRegistry.from_file(_write(tmp_path, body))

    def test_removed_signing_key_field_is_rejected_not_ignored(self, tmp_path):
        # Silently dropping it would leave an operator believing a key they pinned
        # is in use. FastMCP now derives it from client_secret.
        body = """
            [t1]
            tenancy_id      = "ocid1.tenancy.oc1..aaaa"
            idcs_domain     = "idcs-aaaa.identity.oraclecloud.com"
            client_id       = "cid1"
            client_secret   = "sec1"
            audience        = "https://recovery.t1.example.com"
            region          = "us-ashburn-1"
            jwt_signing_key = "deadbeef"
        """
        with pytest.raises(RegistryError, match="jwt_signing_key"):
            TenancyRegistry.from_file(_write(tmp_path, body))

    def test_duplicate_tenancy_id_raises(self, tmp_path):
        body = """
            [t1]
            tenancy_id    = "ocid1.tenancy.oc1..dup"
            idcs_domain   = "idcs-aaaa.identity.oraclecloud.com"
            client_id     = "cid1"
            client_secret = "sec1"
            audience      = "https://recovery.t1.example.com"
            region        = "us-ashburn-1"

            [t2]
            tenancy_id    = "ocid1.tenancy.oc1..dup"
            idcs_domain   = "idcs-bbbb.identity.oraclecloud.com"
            client_id     = "cid2"
            client_secret = "sec2"
            audience      = "https://recovery.t2.example.com"
            region        = "us-phoenix-1"
        """
        with pytest.raises(RegistryError, match="Duplicate tenancy_id"):
            TenancyRegistry.from_file(_write(tmp_path, body))

    def test_reserved_alias_raises(self, tmp_path):
        body = """
            [_select]
            tenancy_id    = "ocid1.tenancy.oc1..aaaa"
            idcs_domain   = "idcs-aaaa.identity.oraclecloud.com"
            client_id     = "cid1"
            client_secret = "sec1"
            audience      = "https://recovery.t1.example.com"
            region        = "us-ashburn-1"
        """
        with pytest.raises(RegistryError, match="reserved"):
            TenancyRegistry.from_file(_write(tmp_path, body))

    def test_non_url_safe_alias_raises(self, tmp_path):
        body = """
            ["bad/alias"]
            tenancy_id    = "ocid1.tenancy.oc1..aaaa"
            idcs_domain   = "idcs-aaaa.identity.oraclecloud.com"
            client_id     = "cid1"
            client_secret = "sec1"
            audience      = "https://recovery.t1.example.com"
            region        = "us-ashburn-1"
        """
        with pytest.raises(RegistryError, match="URL-safe"):
            TenancyRegistry.from_file(_write(tmp_path, body))

    def test_empty_registry_raises(self, tmp_path):
        with pytest.raises(RegistryError, match="empty"):
            TenancyRegistry.from_file(_write(tmp_path, ""))

    def test_http_idcs_domain_rejected(self, tmp_path):
        body = """
            [t1]
            tenancy_id    = "ocid1.tenancy.oc1..aaaa"
            idcs_domain   = "http://idcs-aaaa.identity.oraclecloud.com"
            client_id     = "client-one"
            client_secret = "secret-one"
            audience      = "https://recovery.t1.example.com"
            region        = "us-ashburn-1"
        """
        with pytest.raises(RegistryError, match="https"):
            TenancyRegistry.from_file(_write(tmp_path, body))

    def test_load_registry_requires_path_env(self, monkeypatch):
        monkeypatch.delenv("ORACLE_MCP_TENANCY_REGISTRY", raising=False)
        with pytest.raises(RegistryError, match="ORACLE_MCP_TENANCY_REGISTRY"):
            load_registry()

    def test_load_registry_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORACLE_MCP_TENANCY_REGISTRY", str(tmp_path / "nope.toml"))
        with pytest.raises(RegistryError, match="not found"):
            load_registry()

    def test_load_registry_from_env(self, tmp_path, monkeypatch):
        path = _write(tmp_path, _GOOD)
        monkeypatch.setenv("ORACLE_MCP_TENANCY_REGISTRY", path)
        reg = load_registry()
        assert len(reg) == 2

    def test_legacy_environment_registry_is_cached(self, monkeypatch):
        for name, value in {
            "ORACLE_MCP_IDCS_DOMAIN": "idcs.example.com",
            "ORACLE_MCP_IDCS_CLIENT_ID": "client-id",
            "ORACLE_MCP_IDCS_CLIENT_SECRET": "client-secret",
            "ORACLE_MCP_IDCS_AUDIENCE": "https://recovery.example.com",
            "ORACLE_MCP_TENANCY_ID": "ocid1.tenancy.oc1..example",
            "ORACLE_MCP_REGION": "us-ashburn-1",
            "ORACLE_MCP_TENANCY_ALIAS": "example",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("ORACLE_MCP_TENANCY_REGISTRY", raising=False)
        server._reset_registry_cache()

        registry = server._get_registry()
        assert registry.lookup("example").tenancy_id == "ocid1.tenancy.oc1..example"
        assert registry.lookup("example").audience == "https://recovery.example.com"
        assert server._get_registry() is registry
        server._reset_registry_cache()

    def test_legacy_environment_registry_requires_audience(self, monkeypatch):
        for name, value in {
            "ORACLE_MCP_IDCS_DOMAIN": "idcs.example.com",
            "ORACLE_MCP_IDCS_CLIENT_ID": "client-id",
            "ORACLE_MCP_IDCS_CLIENT_SECRET": "client-secret",
            "ORACLE_MCP_TENANCY_ID": "ocid1.tenancy.oc1..example",
            "ORACLE_MCP_REGION": "us-ashburn-1",
        }.items():
            monkeypatch.setenv(name, value)
        for name in ("ORACLE_MCP_TENANCY_REGISTRY", "ORACLE_MCP_IDCS_AUDIENCE", "IDCS_AUDIENCE"):
            monkeypatch.delenv(name, raising=False)
        server._reset_registry_cache()

        with pytest.raises(RegistryError, match="ORACLE_MCP_IDCS_AUDIENCE"):
            server._get_registry()
        server._reset_registry_cache()

    def test_main_selects_oauth_http_and_stdio_transports(self, monkeypatch):
        run = pytest.MonkeyPatch()
        try:
            run.setattr(server.mcp, "run", lambda **kwargs: kwargs)

            monkeypatch.setenv("ORACLE_MCP_AUTH_METHOD", "oauth")
            monkeypatch.setenv("ORACLE_MCP_HOST", "127.0.0.1")
            monkeypatch.setenv("ORACLE_MCP_PORT", "8601")
            assert server.main() is None

            # session/apikey auth must never serve HTTP: with host+port still set
            # that's a hard startup error now, not a silently-unauthenticated listener.
            monkeypatch.setenv("ORACLE_MCP_AUTH_METHOD", "session")
            with pytest.raises(RuntimeError, match="ORACLE_MCP_AUTH_METHOD=oauth"):
                server.main()

            monkeypatch.delenv("ORACLE_MCP_HOST")
            monkeypatch.delenv("ORACLE_MCP_PORT")
            assert server.main() is None
        finally:
            run.undo()

    def test_request_tenancy_and_token_exchange_signer_are_tenant_scoped(self, monkeypatch):
        for name, value in {
            "ORACLE_MCP_IDCS_DOMAIN": "idcs.example.com",
            "ORACLE_MCP_IDCS_CLIENT_ID": "client-id",
            "ORACLE_MCP_IDCS_CLIENT_SECRET": "client-secret",
            "ORACLE_MCP_IDCS_AUDIENCE": "https://recovery.example.com",
            "ORACLE_MCP_TENANCY_ID": "ocid1.tenancy.oc1..example",
            "ORACLE_MCP_REGION": "us-ashburn-1",
            "ORACLE_MCP_TENANCY_ALIAS": "example",
            "ORACLE_MCP_BASE_URL": "https://mcp.example.com",
            "ORACLE_MCP_AUTH_METHOD": "oauth",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("ORACLE_MCP_TENANCY_REGISTRY", raising=False)
        server._reset_registry_cache()
        token = type(
            "Token",
            (),
            {"token": "access-token", "claims": {TENANT_CLAIM: "example", "jti": "jti-1"}},
        )()
        from fastmcp.server import dependencies
        import oci.auth.signers

        monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
        # Stub only the provider the shared builder constructs (it imports the class
        # into its own namespace); build_idcs_http_auth and the credential handling
        # it returns -- IDCSHttpAuth.context_for -- both run for real.
        with patch("oracle_mcp_common.auth.OCIProvider"):
            monkeypatch.setattr(server.mcp, "auth", server._build_auth_provider(), raising=False)

        class FakeTokenExchangeSigner:
            def __init__(self, jwt_or_func, oci_domain_url, client_id, client_secret, region=None):
                self.values = (jwt_or_func, oci_domain_url, client_id, client_secret, region)

        monkeypatch.setattr(oci.auth.signers, "TokenExchangeSigner", FakeTokenExchangeSigner)

        entry = server._current_tenancy()
        assert entry.alias == "example"
        oauth_config = server._oauth_base_config(entry)
        assert oauth_config["region"] == "us-ashburn-1"
        assert oauth_config["additional_user_agent"] == f"oci-recovery-mcp/{server.__version__}"

        auth_context = server._request_auth_context(entry, oauth_config["region"])
        assert auth_context.config == {"region": "us-ashburn-1"}
        assert auth_context.signer.values == (
            "access-token",
            "https://idcs.example.com",
            "client-id",
            "client-secret",
            "us-ashburn-1",
        )
        # The tenancy's shared IDCS auth policy is built once...
        assert server._tenancy_http_auth(entry) is server._tenancy_http_auth(entry)
        # ...but no signer is cached: a second call for the same tenancy + token
        # builds a distinct signer instead of reusing state outside the request.
        assert (
            server._request_auth_context(entry, oauth_config["region"]).signer
            is not auth_context.signer
        )
        server._reset_registry_cache()

    def test_client_factory_uses_oracle_mcp_common_for_api_key_and_session_authentication(
        self, monkeypatch
    ):
        monkeypatch.setattr(server, "_wrap_oci_client", lambda client, **kwargs: (client, kwargs))
        monkeypatch.setattr(server, "_effective_auth_method", lambda: "apikey")
        monkeypatch.setattr(
            server,
            "_build_profile_auth_context",
            lambda: SimpleNamespace(config={"region": "us-ashburn-1"}, signer="api-key-signer"),
        )
        api_client = server._make_client(
            lambda config, signer: (config, signer),
            region="us-phoenix-1",
            client_name="recovery",
            request_id="request-id",
        )
        assert api_client[0] == (
            {"region": "us-phoenix-1", "additional_user_agent": f"oci-recovery-mcp/{server.__version__}"},
            "api-key-signer",
        )

        monkeypatch.setattr(server, "_effective_auth_method", lambda: "session")
        monkeypatch.setattr(
            server,
            "_build_profile_auth_context",
            lambda: SimpleNamespace(config={"region": "us-ashburn-1"}, signer="session-signer"),
        )
        session_client = server._make_client(
            lambda config, signer: (config, signer), client_name="recovery"
        )
        assert session_client[0] == (
            {"region": "us-ashburn-1", "additional_user_agent": f"oci-recovery-mcp/{server.__version__}"},
            "session-signer",
        )

    def test_server_fallbacks_handle_unserializable_values_and_missing_registry(self, monkeypatch):
        monkeypatch.setattr(
            server.oci.util,
            "to_dict",
            lambda value: (_ for _ in ()).throw(RuntimeError("SDK conversion unavailable")),
        )

        class BrokenDump:
            def model_dump(self, **kwargs):
                raise RuntimeError("dump unavailable")

        class BrokenDict:
            def dict(self, **kwargs):
                raise RuntimeError("dict unavailable")

        class BrokenObject:
            @property
            def __dict__(self):
                raise RuntimeError("attributes unavailable")

        assert server._safe_jsonable(BrokenDump()) == {}
        assert server._safe_jsonable(BrokenDict()) == {}
        assert isinstance(server._safe_jsonable(BrokenObject()), str)

        for name in (
            "ORACLE_MCP_IDCS_DOMAIN", "ORACLE_MCP_IDCS_CLIENT_ID",
            "ORACLE_MCP_IDCS_CLIENT_SECRET", "ORACLE_MCP_TENANCY_ID",
            "ORACLE_MCP_REGION", "ORACLE_MCP_TENANCY_REGISTRY",
        ):
            monkeypatch.delenv(name, raising=False)
        server._reset_registry_cache()
        assert server._legacy_single_tenant_registry() is None
        with pytest.raises(RegistryError, match="oauth mode requires"):
            server._get_registry()

    def test_effective_region_falls_back_when_auth_context_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(server, "_effective_auth_method", lambda: "oauth")
        monkeypatch.setattr(
            server,
            "_current_tenancy",
            lambda: (_ for _ in ()).throw(RuntimeError("no request")),
        )
        monkeypatch.setenv("ORACLE_MCP_REGION", "us-phoenix-1")
        assert server._effective_region("us-ashburn-1") == "us-phoenix-1"

        monkeypatch.setattr(server, "_effective_auth_method", lambda: "session")
        monkeypatch.setattr(
            server,
            "_load_oci_config_for_server",
            lambda: (_ for _ in ()).throw(RuntimeError("no profile")),
        )
        assert server._effective_region("us-ashburn-1") == "us-ashburn-1"
