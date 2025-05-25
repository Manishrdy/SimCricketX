import os
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def load_config():
    config_path = os.path.join(PROJECT_ROOT, "config", "config.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)
