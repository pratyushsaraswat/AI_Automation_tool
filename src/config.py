import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise SystemExit(f"Missing config file: {path}")
    return yaml.safe_load(path.read_text()) or {}


@dataclass
class Settings:
    profile: dict
    search: dict
    llm_provider: str
    outreach: dict

    @property
    def resume_path(self) -> Path:
        return ROOT / self.profile.get("resume_path", "resume/resume.pdf")


def load_settings() -> Settings:
    load_dotenv(CONFIG_DIR / ".env")
    DATA_DIR.mkdir(exist_ok=True)
    return Settings(
        profile=_load_yaml("profile.yaml"),
        search=_load_yaml("search.yaml"),
        llm_provider=os.getenv("LLM_PROVIDER", "none").strip().lower(),
        outreach=_load_yaml("outreach.yaml") if (CONFIG_DIR / "outreach.yaml").exists() else {"enabled": False},
    )
