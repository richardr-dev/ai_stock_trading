"""Settings loader."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@lru_cache
def settings() -> dict:
    with open(ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql+psycopg://localhost:5432/jarvis_stocks")


def market_of(symbol: str) -> str:
    return "ID" if symbol.endswith(".JK") or symbol == "^JKSE" else "US"


def all_tickers() -> dict[str, list[str]]:
    return {m: cfg["tickers"] for m, cfg in settings()["universe"].items()}


def benchmarks() -> dict[str, str]:
    return {m: cfg["benchmark"] for m, cfg in settings()["universe"].items()}
