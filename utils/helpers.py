import os
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def load_config():
    config_path = os.getenv("SIMCRICKETX_CONFIG_PATH") or os.path.join(PROJECT_ROOT, "config", "config.yaml")
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r") as f:
        data = yaml.safe_load(f)
        return data or {}

def safe_print(*args, **kwargs):
    """
    Print function that handles OSError/UnicodeEncodeError on Windows systems
    happening due to emojis or special characters.
    """
    import builtins
    try:
        builtins.print(*args, **kwargs)
    except (OSError, UnicodeEncodeError):
        sanitized = []
        for arg in args:
            if isinstance(arg, str):
                # Remove non-ascii characters or replace them
                sanitized.append(arg.encode("ascii", "ignore").decode())
            else:
                sanitized.append(arg)
        try:
            builtins.print(*sanitized, **kwargs)
        except (OSError, UnicodeEncodeError):
            pass # If it still fails, just suppress it

