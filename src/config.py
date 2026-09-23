"""Configuration loading.

Everything tunable lives in config.yaml. This module loads it once, exposes it
through dotted-path lookup, and layers environment variables on top so that
secrets never sit in the file and deployment can override paths.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class Config:
    """Read-only view over config.yaml with dotted-path access.

    cfg.get("sizing.bands.aggressive.kelly_fraction") beats
    cfg["sizing"]["bands"]["aggressive"]["kelly_fraction"] at every call site,
    and returns a default instead of raising when a key is missing.
    """

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        self._path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        """Same as get(), but raises when the key is absent.

        Use for values the code cannot sensibly default - a silent None that
        propagates into a position size is worse than a loud failure.
        """
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise KeyError(f"Missing required config key: {dotted!r} in {self._path}")
        return value

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.get(name, {}) or {})

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def as_dict(self) -> dict[str, Any]:
        return self._data


@lru_cache(maxsize=1)
def load_config(path: str | Path | None = None) -> Config:
    """Load and cache config.yaml."""
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(data, cfg_path)


# --- Secrets and environment ------------------------------------------------
# These never go in config.yaml: the repo is public on Streamlit Cloud's free
# tier. Locally they come from .env or the shell; in deployment from Streamlit
# secrets and GitHub Actions secrets.


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def anthropic_api_key() -> str | None:
    return _env("ANTHROPIC_API_KEY")


def database_url() -> str:
    """Postgres in deployment, SQLite locally.

    Streamlit Community Cloud has no persistent disk, so a SQLite file there
    would be wiped on every restart. DATABASE_URL must be set in deployment.

    The driver is pinned to pg8000, which is pure Python. psycopg2 ships a
    compiled extension that some locked-down Windows machines refuse to load
    (this one does), and a database driver that works in deployment but not
    on the developer's own machine is a bad trade for a marginal speed gain.
    """
    url = _env("DATABASE_URL")
    if url:
        # Hosted providers hand out postgres://; SQLAlchemy 2.x wants
        # postgresql:// and we want an explicit driver.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+pg8000://", 1)

        # Neon and most hosted Postgres require TLS and express it as
        # ?sslmode=require, which is libpq's spelling. pg8000 does not accept
        # it as a URL parameter and would raise on connect, so it is stripped
        # here and TLS is enabled through connect_args in db.get_engine().
        if "sslmode=" in url:
            import re

            url = re.sub(r"[?&]sslmode=[^&]*", "", url)
            url = url.replace("?&", "?").rstrip("?&")

        return url
    local = load_config().get("database.local_url", "sqlite:///data/screener.db")
    if local.startswith("sqlite:///") and not local.startswith("sqlite:////"):
        rel = local.replace("sqlite:///", "", 1)
        return f"sqlite:///{(PROJECT_ROOT / rel).as_posix()}"
    return local


def telegram_credentials() -> tuple[str | None, str | None]:
    return _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")


def dashboard_password() -> str | None:
    return _env("DASHBOARD_PASSWORD")


def is_deployed() -> bool:
    """True when running anywhere other than a developer's own machine.

    Deliberately biased toward saying yes. This gates the password on the
    dashboard, and the earlier version keyed it on `DATABASE_URL` being set -
    which meant that forgetting *that* secret silently disabled the password
    check on a public URL. A detector guarding access has to fail toward
    "locked", so the only way to get the unguarded path is to ask for it by
    name through ALLOW_INSECURE_LOCAL.
    """
    if _env("ALLOW_INSECURE_LOCAL") in ("1", "true", "yes"):
        return False

    if _env("GITHUB_ACTIONS") == "true" or _env("CI") == "true":
        return True
    if bool(_env("DATABASE_URL")):
        return True

    # Streamlit Community Cloud mounts the repository at /mount/src and sets
    # its own markers. Checking several is cheap; any one of them is enough.
    if _env("STREAMLIT_RUNTIME_ENV") or _env("STREAMLIT_SHARING_MODE"):
        return True
    if Path("/mount/src").exists() or Path("/home/appuser").exists():
        return True
    if _env("HOSTNAME", "").startswith("streamlit"):
        return True

    # A container or server with no sign of a local checkout. Windows and
    # macOS development machines never look like this.
    if os.name == "posix" and not (PROJECT_ROOT / ".git").exists() and _env("HOME") == "/root":
        return True

    return False


def dashboard_access_mode() -> str:
    """How the dashboard should gate access: 'open', 'password', or 'refuse'.

    Three states rather than two, so "no password configured" can be
    distinguished from "no password needed" - conflating them is how the
    earlier version ended up serving a portfolio publicly.
    """
    if dashboard_password():
        return "password"
    if is_deployed():
        return "refuse"
    return "open"


def cache_dir() -> Path:
    cfg = load_config()
    path = PROJECT_ROOT / cfg.get("data.cache_dir", "data/cache")
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_dotenv(path: str | Path | None = None) -> None:
    """Minimal .env loader.

    Avoids a python-dotenv dependency for what is twenty lines. Existing
    environment variables always win, so an explicit export beats the file.
    """
    env_path = Path(path) if path else PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()
