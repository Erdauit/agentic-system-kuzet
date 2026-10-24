"""Контракт утилиты экспорта с rfdetr 1.11.2, без загрузки настоящих весов."""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from tools.models import prepare_rfdetr


def test_export_reads_distribution_version_without_module_dunder_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = ModuleType("rfdetr")  # установленный пакет не имеет __version__

    class Model:
        def export(self, *, output_dir: str, **kwargs: Any) -> Path:
            out = Path(output_dir) / "model.onnx"
            out.write_bytes(b"export-result")
            return out

    module.RFDETRNano = Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rfdetr", module)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.11.2")
    monkeypatch.setattr(sys, "argv", ["prepare_rfdetr", "--size", "nano", "--out", str(tmp_path)])
    assert prepare_rfdetr.main() == 0
    manifest = yaml.safe_load((tmp_path / "rfdetr-person/manifest.yaml").read_text())
    assert manifest["version"] == "1.11.2+nano"
    assert (tmp_path / "rfdetr-person/model.onnx").read_bytes() == b"export-result"
