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
    """Load src.config with an exactly-specified environment.

    Unset variables are set to "" rather than deleted. `src.config` calls
    `load_dotenv()` at import, and `importlib.reload` re-runs it - so a
    deleted variable is immediately restored from the developer's own .env
    file and the test ends up asserting against their real credentials.
    `load_dotenv` skips any key already present in the environment, and ""
    is falsy everywhere the gate logic looks, so this pins the environment
    without depending on whether a .env exists.
    """
    def configure(**values: str):
        for name in GATE_VARS:
            monkeypatch.setenv(name, values.get(name, ""))

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


class TestLibpqParameterStripping:
    """Hosted providers hand out libpq strings; pg8000 is not libpq.

    Neon's default carries both sslmode and channel_binding. Passing either
    to pg8000 raises on connect, so they are stripped here and TLS is applied
    through connect_args instead - which means stripping them and forgetting
    the connect_args would connect in the clear.
    """

    @pytest.mark.parametrize("param", [
        "sslmode=require", "channel_binding=require", "sslrootcert=/x/ca.pem",
        "target_session_attrs=read-write", "gssencmode=disable",
    ])
    def test_each_libpq_parameter_is_removed(self, env, param):
        config = env(DATABASE_URL=f"postgresql://u:p@host/db?{param}")
        url = config.database_url()

        assert param.split("=")[0] not in url
        assert url == "postgresql+pg8000://u:p@host/db"

    def test_the_real_neon_shape(self, env):
        """Exactly what Neon's Connect dialog produces."""
        config = env(
            DATABASE_URL=(
                "postgresql://neondb_owner:npg_secret@ep-little-shape-b3ejpkvi-pooler"
                ".c-4.ap-southeast-1.aws.neon.tech/neondb"
                "?sslmode=require&channel_binding=require"
            )
        )
        url = config.database_url()

        assert url == (
            "postgresql+pg8000://neondb_owner:npg_secret@ep-little-shape-b3ejpkvi"
            "-pooler.c-4.ap-southeast-1.aws.neon.tech/neondb"
        )
        assert "?" not in url and "&" not in url

    def test_parameters_pg8000_understands_are_kept(self, env):
        config = env(
            DATABASE_URL="postgresql://u:p@host/db?sslmode=require&application_name=dip"
        )
        url = config.database_url()

        assert "sslmode" not in url
        assert "application_name=dip" in url
        assert url.count("?") == 1, "the surviving parameter must start a clean query string"


class TestLoginPathIsExecutable:
    """The password comparison shipped with an undefined name.

    A refactor of `authenticated()` removed the line that bound `password`
    but left the comparison using it, so every login attempt raised
    NameError. Nothing caught it because no test imports app.py - it needs a
    Streamlit runtime - and the failure only appears when someone actually
    types a password. These checks are static, so they need no runtime.
    """

    def _authenticated_fn(self):
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("app.py").read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "authenticated"
        )
        return tree, fn

    def test_no_undefined_names_in_the_login_path(self):
        import ast
        import builtins

        tree, fn = self._authenticated_fn()

        bound = {
            node.id for node in ast.walk(fn)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        module_level = {
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        imported = {
            (alias.asname or alias.name).split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        used = {
            node.id for node in ast.walk(fn)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }

        undefined = used - bound - module_level - imported - set(dir(builtins))
        assert not undefined, f"authenticated() references undefined names: {undefined}"

    def test_the_comparison_is_constant_time(self):
        """== leaks length and prefix through timing; this is the only lock."""
        import pathlib

        source = pathlib.Path("app.py").read_text(encoding="utf-8")
        assert "compare_digest" in source
        assert "if entered == password" not in source

    def test_an_empty_submission_cannot_pass(self):
        """compare_digest("", "") is True, so the empty case needs its own guard."""
        import pathlib

        source = pathlib.Path("app.py").read_text(encoding="utf-8")
        assert "if entered and secrets.compare_digest" in source, (
            "an empty password must be rejected before the digest comparison"
        )
