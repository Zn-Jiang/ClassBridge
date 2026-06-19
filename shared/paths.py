from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "configs"
LOG_DIR = ROOT_DIR / "logs"
DATA_DIR = ROOT_DIR / "data"
NONEBOT_DIR = ROOT_DIR / "Nonebot" / "kgGao29Robot"
PLUGIN_DIR = NONEBOT_DIR / "src" / "plugins" / "message_handler_plugin"

# Per-component config paths
SERVER_CONFIG_PATH = CONFIG_DIR / "server.toml"
SERVER_EXAMPLE_CONFIG_PATH = CONFIG_DIR / "server.example.toml"
CLIENT_CONFIG_PATH = CONFIG_DIR / "client.toml"
CLIENT_EXAMPLE_CONFIG_PATH = CONFIG_DIR / "client.example.toml"
PLUGIN_CONFIG_PATH = CONFIG_DIR / "plugin.toml"
PLUGIN_EXAMPLE_CONFIG_PATH = CONFIG_DIR / "plugin.example.toml"


def ensure_runtime_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

