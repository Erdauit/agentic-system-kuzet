"""Лицензионный гейт (SPEC.md §2.7): запрещённые и неизвестные лицензии валят CI.

Тесты строят фальшивые установленные пакеты (*.dist-info/METADATA) во временном каталоге —
это тот же путь, по которому гейт читает настоящее окружение.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.licenses import Verdict, check_npm, check_python, load_policy, parse, render
from shared.licenses.gate import declared_python_licenses, main
from shared.licenses.spdx import Policy, SpdxParseError

POLICY = load_policy()


def _fake_dist(site: Path, name: str, version: str = "1.0", **fields: str | list[str]) -> None:
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    for key, value in fields.items():
        header = key.replace("_", "-")
        for v in value if isinstance(value, list) else [value]:
            lines.append(f"{header}: {v}")
    (info / "METADATA").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- SPDX


@pytest.mark.parametrize(
    ("expr", "verdict"),
    [
        ("MIT", Verdict.ALLOWED),
        ("mit", Verdict.ALLOWED),
        ("Apache-2.0 WITH LLVM-exception", Verdict.ALLOWED),
        ("MIT OR GPL-3.0-only", Verdict.ALLOWED),
        ("MIT AND GPL-3.0-only", Verdict.DENIED),
        ("AGPL-3.0-or-later", Verdict.DENIED),
        ("SSPL-1.0", Verdict.DENIED),
        ("LicenseRef-RSALv2", Verdict.DENIED),
        ("LGPL-2.1-or-later", Verdict.UNKNOWN),
        ("(MIT OR Apache-2.0) AND BSD-3-Clause", Verdict.ALLOWED),
        ("GPL-2.0-only WITH Classpath-exception-2.0", Verdict.DENIED),
    ],
)
def test_spdx_evaluation(expr: str, verdict: Verdict) -> None:
    assert POLICY.spdx.evaluate(parse(expr)) is verdict


def test_spdx_render_round_trip() -> None:
    assert render(parse("(MIT OR Apache-2.0) AND ISC")) == "(MIT OR Apache-2.0) AND ISC"


@pytest.mark.parametrize("bad", ["MIT OR", "(MIT", "AND MIT", "MIT License Agreement"])
def test_spdx_parse_errors(bad: str) -> None:
    with pytest.raises(SpdxParseError):
        parse(bad)


def test_policy_canonical_case() -> None:
    p = Policy(allowed=frozenset({"Apache-2.0"}), denied_patterns=())
    assert p.classify_id("apache-2.0") is Verdict.ALLOWED


# ---------------------------------------------------------------- Python


def test_python_gate_blocks_agpl_package(tmp_path: Path) -> None:
    """Приёмка Фазы 0: пакет с запрещённой лицензией валит гейт."""
    _fake_dist(tmp_path, "good-lib", License_Expression="MIT")
    _fake_dist(
        tmp_path,
        "ultralytics",
        Classifier="License :: OSI Approved :: GNU Affero General Public License v3",
    )
    decisions = {d.package: d for d in check_python(POLICY, [str(tmp_path)])}
    assert decisions["good-lib"].verdict is Verdict.ALLOWED
    assert decisions["ultralytics"].verdict is Verdict.DENIED
    assert main(["python", "--path", str(tmp_path)]) == 1


def test_python_gate_blocks_unknown_license(tmp_path: Path) -> None:
    _fake_dist(tmp_path, "mystery")
    _fake_dist(tmp_path, "custom", License="Copyright ACME, all rights reserved")
    decisions = {d.package: d for d in check_python(POLICY, [str(tmp_path)])}
    assert decisions["mystery"].verdict is Verdict.UNKNOWN
    assert decisions["custom"].verdict is Verdict.UNKNOWN
    assert main(["python", "--path", str(tmp_path)]) == 1


def test_python_gate_passes_clean_env(tmp_path: Path) -> None:
    _fake_dist(tmp_path, "a", License_Expression="Apache-2.0")
    _fake_dist(tmp_path, "b", Classifier=["License :: OSI Approved :: BSD License"])
    _fake_dist(tmp_path, "qoima-internal", License_Expression="LicenseRef-Qoima-Proprietary")
    decisions = check_python(POLICY, [str(tmp_path)])
    assert [d.package for d in decisions] == ["a", "b"]  # first-party пропущен
    assert main(["python", "--path", str(tmp_path)]) == 0


def test_reviewed_exception_is_pinned_to_license(tmp_path: Path) -> None:
    _fake_dist(tmp_path, "certifi", License_Expression="MPL-2.0")
    assert check_python(POLICY, [str(tmp_path)])[0].verdict is Verdict.REVIEWED
    other = tmp_path / "other"
    _fake_dist(other, "certifi", License_Expression="GPL-3.0-only")  # «сменил лицензию»
    assert check_python(POLICY, [str(other)])[0].verdict is Verdict.DENIED


def test_full_license_text_recognized(tmp_path: Path) -> None:
    text = "Copyright (c) 2020 X\n Permission is hereby granted, free of charge, to any person"
    _fake_dist(tmp_path, "texty")
    meta = tmp_path / "texty-1.0.dist-info" / "METADATA"
    meta.write_text(meta.read_text() + "License: " + text.replace("\n", "\n        ") + "\n")
    import importlib.metadata as md

    dist = next(iter(md.distributions(path=[str(tmp_path)])))
    assert declared_python_licenses(dist.metadata) == ["MIT"]


def test_current_environment_passes() -> None:
    """Реальное окружение разработки проходит гейт (то же проверяет CI)."""
    blocking = [d for d in check_python(POLICY) if d.verdict in (Verdict.DENIED, Verdict.UNKNOWN)]
    assert blocking == []


# ---------------------------------------------------------------- npm


def _lockfile(tmp_path: Path, packages: dict[str, dict[str, object]]) -> Path:
    path = tmp_path / "package-lock.json"
    path.write_text(
        json.dumps({"lockfileVersion": 3, "packages": {"": {"name": "qoima-ui"}, **packages}})
    )
    return path


def test_npm_gate(tmp_path: Path) -> None:
    lock = _lockfile(
        tmp_path,
        {
            "node_modules/react": {"version": "19.0.0", "license": "MIT"},
            "node_modules/@scope/dual": {"version": "1.0.0", "license": "(MIT OR CC0-1.0)"},
            "node_modules/bad": {"version": "2.0.0", "license": "GPL-3.0"},
            "node_modules/nolicense": {"version": "0.1.0"},
        },
    )
    decisions = {d.package: d for d in check_npm(POLICY, lock)}
    assert decisions["react"].verdict is Verdict.ALLOWED
    assert decisions["@scope/dual"].verdict is Verdict.ALLOWED
    assert decisions["bad"].verdict is Verdict.DENIED
    assert decisions["nolicense"].verdict is Verdict.UNKNOWN
    assert main(["npm", str(lock)]) == 1


def test_repository_ui_lockfile_passes(repo_root: Path) -> None:
    lock = repo_root / "ui" / "package-lock.json"
    assert main(["npm", str(lock)]) == 0
