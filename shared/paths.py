import os
import sys
from pathlib import Path


def _resolve_root() -> Path:
    """Return the application root directory.

    In development this is the project root (parent of ``shared/``).
    When compiled (Nuitka / PyInstaller) this is the directory containing the
    executable, so that config files placed alongside the exe are found.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_appdata_dir() -> Path:
    """Return the per-user application data directory for the client.

    On Windows this is ``%APPDATA%/ClassBridge``.
    On other platforms this is ``~/.classbridge``.
    """
    appdata_root = os.environ.get("APPDATA")
    if appdata_root:
        return Path(appdata_root) / "新江" / "ClassBridge"
    return Path.home() / ".classbridge"


ROOT_DIR = _resolve_root()
CONFIG_DIR = ROOT_DIR / "configs"  # kept for backward-compat reference only
LOG_DIR = ROOT_DIR / "logs"
DATA_DIR = ROOT_DIR / "data"
NONEBOT_DIR = ROOT_DIR / "Nonebot" / "kgGao29Robot"
PLUGIN_DIR = NONEBOT_DIR / "src" / "plugins" / "message_handler_plugin"

# Per-component config paths (each lives inside its own module directory)
SERVER_CONFIG_PATH = ROOT_DIR / "server" / "server.toml"
SERVER_EXAMPLE_CONFIG_PATH = ROOT_DIR / "server" / "server.example.toml"
PLUGIN_CONFIG_PATH = NONEBOT_DIR / "plugin.toml"
PLUGIN_EXAMPLE_CONFIG_PATH = NONEBOT_DIR / "plugin.example.toml"

# Client data lives in the per-user appdata directory (Windows convention).
CLIENT_APPDATA_DIR = _resolve_appdata_dir()
CLIENT_CONFIG_PATH = CLIENT_APPDATA_DIR / "client.toml"
CLIENT_EXAMPLE_CONFIG_PATH = ROOT_DIR / "client" / "client.example.toml"
CLIENT_DATABASE_PATH = CLIENT_APPDATA_DIR / "client.db"
# Persisted queue of read receipts that still need to be synced to the server.
# Stored on disk (not RAM) so a shutdown while offline doesn't lose them.
CLIENT_PENDING_READS_PATH = CLIENT_APPDATA_DIR / "pending_reads.json"


def ensure_runtime_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def ensure_client_appdata_dir() -> Path:
    """Create and return the client appdata directory."""
    CLIENT_APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    return CLIENT_APPDATA_DIR
