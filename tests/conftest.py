"""Общие фикстуры тестов."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.config import QoimaConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "configs"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def config() -> QoimaConfig:
    return load_config(CONFIG_DIR)
