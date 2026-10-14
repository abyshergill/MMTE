from pathlib import Path

APP_NAME = "Modern Multi Terminal Emulator"
CONFIG_DIR = Path.home() / ".modern_multi_terminal_emulator"
PROFILES_FILE = CONFIG_DIR / "ssh_profiles.json"
TELNET_PROFILES_FILE = CONFIG_DIR / "telnet_profiles.json"
LOG_DIR = CONFIG_DIR / "logs"