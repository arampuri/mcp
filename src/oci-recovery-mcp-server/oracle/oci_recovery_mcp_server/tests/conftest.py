"""
Copyright (c) 2025, Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at
https://oss.oracle.com/licenses/upl.
"""

import pytest


@pytest.fixture(autouse=True)
def fastmcp_home(tmp_path_factory, monkeypatch):
    """Keep FastMCP's persisted OAuth state inside the test's temp directory.

    Building the OAuth provider makes FastMCP create its encrypted OAuth-state
    store under `settings.home`. That default is the real user data directory, so
    without this the suite would write (and leave behind) state there. The settings
    singleton is constructed at import time, so FASTMCP_HOME is set too late to
    help; patch the resolved attribute instead.
    """
    import fastmcp

    monkeypatch.setattr(fastmcp.settings, "home", tmp_path_factory.mktemp("fastmcp"))
