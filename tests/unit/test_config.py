"""Конфиги: реальные configs/ валидны, ошибки понятны, переопределения объекта работают."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from shared.config import ConfigError, QoimaConfig, deep_merge, load_config
from shared.schemas import ScenarioKind
from tests.conftest import CONFIG_DIR


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    target = tmp_path / "configs"
    shutil.copytree(CONFIG_DIR, target)
    return target


def _patch(path: Path, mutate: object) -> None:
    data = yaml.safe_load(path.read_text())
    mutate(data)  # type: ignore[operator]
    path.write_text(yaml.safe_dump(data, allow_unicode=True))


def test_repository_configs_are_valid(config: QoimaConfig) -> None:
    assert set(config.scenarios) == set(ScenarioKind)
    assert config.defaults.slo.camera_offline_detection_s == 15
    assert config.defaults.vlm.arbiter_timeout_s == 4.0


def test_offline_detection_must_fit_slo(config_copy: Path) -> None:
    _patch(config_copy / "defaults.yaml", lambda d: d["camera_health"].update(stream_timeout_s=12))
    with pytest.raises(ConfigError) as exc:
        load_config(config_copy)
    assert any("превышает SLO" in e for e in exc.value.errors)


def test_unknown_key_reported_with_file(config_copy: Path) -> None:
    _patch(config_copy / "defaults.yaml", lambda d: d["slo"].update(fall_to_alrt_p95_s=2))
    with pytest.raises(ConfigError) as exc:
        load_config(config_copy)
    assert any("defaults.yaml" in e and "fall_to_alrt_p95_s" in e for e in exc.value.errors)


def test_scenario_fps_range_enforced(config_copy: Path) -> None:
    _patch(config_copy / "scenarios" / "smoke_fire.yaml", lambda d: d.update(target_fps=10))
    with pytest.raises(ConfigError) as exc:
        load_config(config_copy)
    assert any("smoke_fire.yaml" in e and "target_fps" in e for e in exc.value.errors)


def test_safety_class_cannot_be_disabled(config_copy: Path) -> None:
    _patch(config_copy / "scenarios" / "fall.yaml", lambda d: d.update(safety_class=False))
    with pytest.raises(ConfigError, match="safety_class"):
        load_config(config_copy)


def test_system_alerts_not_routed_to_operators(config_copy: Path) -> None:
    _patch(config_copy / "alerts.yaml", lambda d: d["system"].update(groups=["shift_operators"]))
    with pytest.raises(ConfigError, match="системные алерты"):
        load_config(config_copy)


def test_invalid_timezone(config_copy: Path) -> None:
    _patch(config_copy / "defaults.yaml", lambda d: d["site"].update(timezone="Mars/Olympus"))
    with pytest.raises(ConfigError, match="часовой пояс"):
        load_config(config_copy)


def test_credentials_in_url_template_rejected(config_copy: Path) -> None:
    _patch(
        config_copy / "camera_url_templates.yaml",
        lambda d: d["vendors"]["hikvision"].update(main="rtsp://admin:pass@{host}:{port}/x"),
    )
    with pytest.raises(ConfigError, match="учётные данные"):
        load_config(config_copy)


def test_site_overrides_merge(config_copy: Path) -> None:
    (config_copy / "site.yaml").write_text(
        yaml.safe_dump({"defaults": {"site": {"name": "Цех №3"}}}, allow_unicode=True)
    )
    cfg = load_config(config_copy)
    assert cfg.defaults.site.name == "Цех №3"
    assert cfg.defaults.site.timezone == "Asia/Almaty"  # остальное — из defaults.yaml


def test_site_unknown_section_rejected(config_copy: Path) -> None:
    (config_copy / "site.yaml").write_text("pipelines: {}\n")
    with pytest.raises(ConfigError, match="неизвестные разделы"):
        load_config(config_copy)


def test_deep_merge_replaces_lists() -> None:
    merged = deep_merge({"a": {"b": 1, "c": [1, 2]}}, {"a": {"c": [3]}})
    assert merged == {"a": {"b": 1, "c": [3]}}
