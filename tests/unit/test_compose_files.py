"""Compose-файлы соблюдают принципы SPEC.md: лицензии образов, версии, сети, healthcheck, секреты."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from shared.licenses import load_policy, parse
from shared.licenses.spdx import Verdict

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "compose"
FILES = sorted(COMPOSE_DIR.glob("compose*.yml"))
COMPONENTS: dict[str, dict[str, Any]] = yaml.safe_load(
    resources.files("shared.licenses").joinpath("components.yaml").read_text(encoding="utf-8")
)["images"]
POLICY = load_policy()


def _services() -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for f in FILES:
        data = yaml.safe_load(f.read_text())
        for name, svc in (data.get("services") or {}).items():
            out.append((f.name, name, svc))
    return out


def _repo_and_tag(image: str) -> tuple[str, str]:
    # Тег — после первого ':' за последним '/': так корректно разбираются и
    # nvcr.io/nvidia/tritonserver:26.08-py3, и qoima/core-api:${QOIMA_VERSION:-dev}.
    slash = image.rfind("/")
    colon = image.find(":", slash + 1)
    if colon == -1:
        return image, ""
    return image[:colon], image[colon + 1 :]


def test_compose_files_exist() -> None:
    assert [f.name for f in FILES] == [
        "compose.engineering.yml",
        "compose.gpu1.yml",
        "compose.gpu2.yml",
        "compose.sim.yml",
        "compose.yml",
    ]


@pytest.mark.parametrize(("file", "name", "svc"), [s for s in _services() if "image" in s[2]])
def test_images_pinned_and_licensed(file: str, name: str, svc: dict[str, Any]) -> None:
    repo, tag = _repo_and_tag(svc["image"])
    assert tag and tag != "latest", f"{name}: образ должен быть закреплён на версии"
    entry = COMPONENTS.get(repo)
    assert entry is not None, f"{repo} нет в shared/licenses/components.yaml"
    if entry.get("first_party"):
        assert tag.startswith("${QOIMA_VERSION")
        if entry.get("only_in"):
            assert file in entry["only_in"], f"{repo} разрешён только в {entry['only_in']}"
        return
    assert any(ch.isdigit() for ch in tag), f"{name}: тег {tag} без версии"
    only_in = entry.get("only_in")
    if only_in:
        assert file in only_in, f"{repo} ({entry['license']}) разрешён только в {only_in}"
        return
    verdict = POLICY.spdx.evaluate(parse(entry["license"]))
    assert verdict is Verdict.ALLOWED, f"{repo}: лицензия {entry['license']} не разрешена"


def test_grafana_only_in_engineering_profile() -> None:
    for file, _name, svc in _services():
        if "grafana" in str(svc.get("image", "")):
            assert file == "compose.engineering.yml"


def test_base_services_have_healthchecks() -> None:
    base = yaml.safe_load((COMPOSE_DIR / "compose.yml").read_text())
    gpu = yaml.safe_load((COMPOSE_DIR / "compose.gpu1.yml").read_text())
    for name, svc in {
        **base["services"],
        # Новые сервисы GPU-профиля; переопределения базовых (образ vision-pipeline-gpu)
        # наследуют healthcheck базового файла.
        **{k: v for k, v in gpu["services"].items() if "image" in v and k not in base["services"]},
    }.items():
        if "build" in svc.get("profiles", []):
            continue  # media-base: только источник для сборки, не запускается
        assert "healthcheck" in svc, f"{name}: нет healthcheck — нельзя дождаться healthy"


def test_network_segmentation() -> None:
    base = yaml.safe_load((COMPOSE_DIR / "compose.yml").read_text())
    assert base["networks"]["backend"]["internal"] is True
    edge = {n for n, s in base["services"].items() if "edge" in s.get("networks", [])}
    # Наружу смотрят только UI/API и рестример камер (SPEC.md §10).
    assert edge == {"core-api", "mediamtx"}
    published = {n: s["ports"] for n, s in base["services"].items() if s.get("ports")}
    assert set(published) == {"core-api", "mediamtx"}
    # MediaMTX публикует только медиапорт WebRTC; API, RTSP и сигнализация — внутри backend.
    assert all(str(p).startswith("8189:8189") for p in published["mediamtx"])


def test_no_inline_secrets() -> None:
    """Пароли и токены передаются только файлами Docker secrets (SPEC.md §10)."""
    sensitive = ("PASSWORD", "TOKEN", "SECRET", "API_KEY")
    for file, name, svc in _services():
        if file == "compose.sim.yml":
            continue  # симулятор камер для разработки: пароль admin/qoima-sim — тестовый, задокументирован
        env = svc.get("environment") or {}
        items = env.items() if isinstance(env, dict) else (e.split("=", 1) for e in env)
        for key, value in items:
            if any(s in str(key).upper() for s in sensitive):
                # Только ссылки на файлы и каталоги секретов, никаких значений в открытом виде.
                assert str(value).startswith("/run/"), (
                    f"{file}:{name}: {key} должен ссылаться на /run/…"
                )


def test_gpu_profiles_split_worker() -> None:
    def worker_device(name: str) -> list[str]:
        data = yaml.safe_load((COMPOSE_DIR / name).read_text())
        devices = data["services"]["worker-gpu"]["deploy"]["resources"]["reservations"]["devices"]
        return list(devices[0]["device_ids"])

    assert worker_device("compose.gpu1.yml") == ["0"]
    assert worker_device("compose.gpu2.yml") == ["1"]  # SPEC.md §4.1: GPU1 — worker-gpu
