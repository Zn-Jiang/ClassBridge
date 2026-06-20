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


ROOT_DIR = _resolve_root()
CONFIG_DIR = ROOT_DIR / "configs"  # kept for backward-compat reference only
LOG_DIR = ROOT_DIR / "logs"
DATA_DIR = ROOT_DIR / "data"
NONEBOT_DIR = ROOT_DIR / "Nonebot" / "kgGao29Robot"
PLUGIN_DIR = NONEBOT_DIR / "src" / "plugins" / "message_handler_plugin"

# Per-component config paths (each lives inside its own module directory)
SERVER_CONFIG_PATH = ROOT_DIR / "server" / "server.toml"
SERVER_EXAMPLE_CONFIG_PATH = ROOT_DIR / "server" / "server.example.toml"
CLIENT_CONFIG_PATH = ROOT_DIR / "client" / "client.toml"
CLIENT_EXAMPLE_CONFIG_PATH = ROOT_DIR / "client" / "client.example.toml"
PLUGIN_CONFIG_PATH = NONEBOT_DIR / "plugin.toml"
PLUGIN_EXAMPLE_CONFIG_PATH = NONEBOT_DIR / "plugin.example.toml"


def ensure_runtime_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
