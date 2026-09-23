"""Dashboard access-gate tests.

The earlier version of this gate failed **open**: it keyed "is this deployed"
on `DATABASE_URL` being set, so forgetting that one secret silently disabled
the password check on a public URL. These tests pin the corrected behaviour,
because the cost of a regression here is someone's portfolio being served to
anyone who guesses the address.
"""

from __future__ import annotations

import importlib
import os

import pytest

GATE_VARS = (
    "DATABASE_URL", "GITHUB_ACTIONS", "CI", "DASHBOARD_PASSWORD",
    "ALLOW_INSECURE_LOCAL", "STREAMLIT_RUNTIME_ENV", "STREAMLIT_SHARING_MODE",
)


@pytest.fixture
def env(monkeypatch):
    """Load src.config with an exactly-specified environment."""
    def configure(**values: str):
        for name in GATE_VARS:
            monkeypatch.delenv(name, raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

        import src.config as config
        importlib.reload(config)
        return config

    return configure


class TestFailsClosed:
    def test_deployed_without_a_password_refuses(self, env):
        """The whole point. No password on a public URL must stop the app."""
        config = env(STREAMLIT_RUNTIME_ENV="cloud")
        assert config.dashboard_access_mode() == "refuse"

    def test_the_original_hole_is_shut(self, env):
        """Password set, DATABASE_URL forgotten.

        This combination used to report 'not deployed' and serve openly even
        though a password had been configured.
        """
        config = env(DASHBOARD_PASSWORD="hunter2", STREAMLIT_RUNTIME_ENV="cloud")
        assert config.is_deployed() is True
        assert config.dashboard_access_mode() == "password"

    def test_both_secrets_forgotten_refuses(self, env):
        config = env(STREAMLIT_RUNTIME_ENV="cloud")
        assert config.dashboard_access_mode() == "refuse"

    def test_ci_without_a_password_refuses(self, env):
        assert env(GITHUB_ACTIONS="true").dashboard_access_mode() == "refuse"
        assert env(CI="true").dashboard_access_mode() == "refuse"

    def test_a_database_url_alone_still_counts_as_deployed(self, env):
        config = env(DATABASE_URL="postgresql://user:pw@host/db")
        assert config.is_deployed() is True
        assert config.dashboard_access_mode() == "refuse"


class TestLocalDevelopment:
    def test_a_bare_local_machine_is_open(self, env):
        """A developer with nothing configured should not be locked out."""
        assert env().dashboard_access_mode() == "open"

    def test_the_insecure_override_must_be_asked_for_by_name(self, env):
        config = env(ALLOW_INSECURE_LOCAL="1", STREAMLIT_RUNTIME_ENV="cloud")
        assert config.is_deployed() is False
        assert config.dashboard_access_mode() == "open"

    def test_the_override_accepts_only_explicit_affirmatives(self, env):
        for value in ("0", "false", "no", ""):
            config = env(ALLOW_INSECURE_LOCAL=value, STREAMLIT_RUNTIME_ENV="cloud")
            assert config.dashboard_access_mode() == "refuse", f"{value!r} should not unlock"

    def test_a_password_is_honoured_locally_too(self, env):
        assert env(DASHBOARD_PASSWORD="hunter2").dashboard_access_mode() == "password"


class TestDeploymentDetection:
    @pytest.mark.parametrize("marker", [
        {"STREAMLIT_RUNTIME_ENV": "cloud"},
        {"STREAMLIT_SHARING_MODE": "on"},
        {"GITHUB_ACTIONS": "true"},
        {"CI": "true"},
        {"DATABASE_URL": "postgresql://x"},
    ])
    def test_each_marker_is_enough_on_its_own(self, env, marker):
        assert env(**marker).is_deployed() is True

    def test_database_url_normalises_the_postgres_scheme(self, env):
        """Hosted providers hand out postgres://; SQLAlchemy 2 needs postgresql://."""
        config = env(DATABASE_URL="postgres://user:pw@host:5432/db")
        assert config.database_url().startswith("postgresql+pg8000://")

    def test_the_driver_is_pinned_explicitly(self, env):
        """pg8000 is pure Python; psycopg2's DLL is blocked on the dev machine."""
        config = env(DATABASE_URL="postgresql://user:pw@host:5432/db")
        assert "+pg8000" in config.database_url()

    def test_libpq_sslmode_is_stripped(self, env):
        """Neon appends ?sslmode=require, which pg8000 rejects as a URL param.

        TLS is applied through connect_args in db.get_engine() instead, so
        stripping it here must not mean connecting in the clear.
        """
        config = env(
            DATABASE_URL="postgresql://u:p@ep-x.aws.neon.tech/neondb?sslmode=require"
        )
        url = config.database_url()

        assert "sslmode" not in url
        assert not url.endswith(("?", "&"))
        assert url == "postgresql+pg8000://u:p@ep-x.aws.neon.tech/neondb"

    def test_other_query_parameters_survive_the_strip(self, env):
        config = env(
            DATABASE_URL="postgresql://u:p@host/db?sslmode=require&application_name=dip"
        )
        url = config.database_url()

        assert "sslmode" not in url
        assert "application_name=dip" in url

    def test_a_local_sqlite_url_is_left_alone(self, env):
        config = env()
        assert config.database_url().startswith("sqlite:///")
