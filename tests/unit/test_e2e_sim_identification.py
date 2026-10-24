"""Неисправности симулятора должны работать с IP и постоянным Docker DNS-именем."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.e2e import test_phase1_cameras as cameras


@pytest.mark.parametrize("host", ["172.18.0.2", "qoima-camera-sim-2"])
def test_simulator_identification(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def inspect(command: list[str], **kwargs: Any) -> SimpleNamespace:
        index = command[-1].rsplit("-", 1)[1]
        return SimpleNamespace(stdout=f"172.18.0.{index} ")

    monkeypatch.setattr(cameras.subprocess, "run", inspect)
    assert cameras._sim_index({"camera": {"main_url": f"rtsp://{host}:554/stream"}}) == 2
