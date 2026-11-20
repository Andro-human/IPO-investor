"""Configuration loading: config.toml + .env (secrets)."""
import os
import pathlib
import re

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECRET_KEYS = ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_TOTP_SECRET", "UPSTOX_PIN",
               "UPSTOX_UPI", "TELEGRAM_BOT_TOKEN")


def load_env(path: pathlib.Path = ROOT / ".env") -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = re.sub(r"\s+#.*$", "", v)  # drop inline comments
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def load_config(path=None) -> dict:
    path = pathlib.Path(path or os.environ.get("IPO_CONFIG", ROOT / "config.toml"))
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    load_env()
    cfg["secrets"] = {k: os.environ.get(k, "") for k in SECRET_KEYS}
    cfg["_root"] = ROOT
    cfg.setdefault("general", {}).setdefault("state_dir", "state")
    sd = pathlib.Path(cfg["general"]["state_dir"])
    cfg["_state_dir"] = sd if sd.is_absolute() else ROOT / sd
    cfg["_state_dir"].mkdir(parents=True, exist_ok=True)
    return cfg


def missing_secrets(cfg: dict, keys=SECRET_KEYS) -> list:
    return [k for k in keys if not cfg["secrets"].get(k)]
