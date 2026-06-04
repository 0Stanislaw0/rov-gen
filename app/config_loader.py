import yaml
from pathlib import Path

def load_config(config_path: str):
    """Загрузка YAML конфигурации."""
    if not Path(config_path).exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
