import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Secrets:
    wp_url: str
    wp_user: str
    wp_app_password: str


def load_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_secrets(require: bool = True) -> Secrets:
    s = Secrets(
        wp_url=os.environ.get("WP_URL", "").rstrip("/"),
        wp_user=os.environ.get("WP_USER", ""),
        wp_app_password=os.environ.get("WP_APP_PASSWORD", ""),
    )
    if require:
        missing = [k for k, v in vars(s).items() if not v]
        if missing:
            raise SystemExit("Missing secrets: " + ", ".join(missing) + ". Add them in GitHub > Settings > Secrets.")
    return s
