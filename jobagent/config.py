"""Конфигурация: .env, profile.yaml, пути."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "profile.yaml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobs.db"

load_dotenv(ROOT / ".env")


def load_profile(path: Path = PROFILE_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env(name: str) -> str:
    return os.getenv(name, "").strip()


def owner_chat_id() -> int | None:
    raw = env("TELEGRAM_CHAT_ID")
    try:
        return int(raw)
    except ValueError:
        return None
