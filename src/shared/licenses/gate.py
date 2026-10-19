"""Лицензионный гейт CI (SPEC.md §2.7, §12 Фаза 0).

    qoima-license-gate python [--path DIR ...] [--format text|markdown|json]
    qoima-license-gate npm ui/package-lock.json [--format ...]

Код возврата: 0 — все зависимости разрешены или прошли ревью; 1 — есть запрещённые или
неизвестные лицензии; 2 — ошибка использования.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

from shared.licenses.spdx import Policy, SpdxParseError, Verdict, parse, render

Ecosystem = Literal["python", "npm"]


@dataclass(frozen=True)
class ReviewedException:
    ecosystem: Ecosystem
    package: str
    license: str
    reason: str
    reviewed_by: str


@dataclass(frozen=True)
class GatePolicy:
    spdx: Policy
    aliases: dict[str, str]
    python_first_party_prefixes: tuple[str, ...]
    npm_first_party_names: frozenset[str]
    reviewed: tuple[ReviewedException, ...]

    def alias(self, raw: str) -> str:
        return self.aliases.get(raw.strip().lower(), raw.strip())

    def find_review(
        self, ecosystem: Ecosystem, package: str, expression: str
    ) -> ReviewedException | None:
        for r in self.reviewed:
            if (
                r.ecosystem == ecosystem
                and canonical_name(ecosystem, r.package) == canonical_name(ecosystem, package)
                and r.license == expression
            ):
                return r
        return None


def canonical_name(ecosystem: Ecosystem, name: str) -> str:
    """PEP 503 для Python (typing_extensions == typing-extensions); npm-имена — как есть."""
    if ecosystem == "python":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


def load_policy(path: Path | None = None) -> GatePolicy:
    if path is None:
        text = (
            resources.files("shared.licenses").joinpath("policy.yaml").read_text(encoding="utf-8")
        )
    else:
        text = path.read_text(encoding="utf-8")
    raw: dict[str, Any] = yaml.safe_load(text)
    first_party = raw.get("first_party", {})
    return GatePolicy(
        spdx=Policy(
            allowed=frozenset(raw["allowed"]),
            denied_patterns=tuple(raw["denied_patterns"]),
        ),
        aliases={str(k).strip().lower(): str(v) for k, v in raw.get("aliases", {}).items()},
        python_first_party_prefixes=tuple(first_party.get("python_prefixes", [])),
        npm_first_party_names=frozenset(first_party.get("npm_names", [])),
        reviewed=tuple(ReviewedException(**r) for r in raw.get("reviewed") or []),
    )


@dataclass(frozen=True)
class Decision:
    ecosystem: Ecosystem
    package: str
    version: str
    declared: str
    expression: str | None
    verdict: Verdict
    reason: str


def _normalize(policy: GatePolicy, raw: str) -> str:
    """Строку лицензии → SPDX-выражение: сначала алиас целиком, затем алиасы отдельных id."""
    return render(parse(policy.alias(raw)))


def decide(
    policy: GatePolicy, ecosystem: Ecosystem, package: str, version: str, declared: list[str]
) -> Decision:
    """Решение по пакету. declared — список заявленных лицензий (все должны выполняться)."""
    shown = " | ".join(declared) if declared else "<не указана>"
    if not declared:
        return Decision(
            ecosystem, package, version, shown, None, Verdict.UNKNOWN, "лицензия не указана"
        )
    parts: list[str] = []
    for item in declared:
        try:
            parts.append(_normalize(policy, item))
        except SpdxParseError:
            return Decision(
                ecosystem,
                package,
                version,
                shown,
                None,
                Verdict.UNKNOWN,
                f"не удалось распознать лицензию {item!r}",
            )
    unique = list(dict.fromkeys(parts))
    expression = (
        unique[0] if len(unique) == 1 else " AND ".join(f"({p})" if " " in p else p for p in unique)
    )
    verdict = policy.spdx.evaluate(parse(expression))
    if verdict is Verdict.ALLOWED:
        return Decision(ecosystem, package, version, shown, expression, verdict, "в белом списке")
    review = policy.find_review(ecosystem, package, expression)
    if review is not None:
        return Decision(
            ecosystem, package, version, shown, expression, Verdict.REVIEWED, review.reason
        )
    reason = (
        "запрещённая лицензия" if verdict is Verdict.DENIED else "нет в белом списке, нужно ревью"
    )
    return Decision(ecosystem, package, version, shown, expression, verdict, reason)


# ---------------------------------------------------------------- Python

_CLASSIFIER_PREFIX = "License :: "


def _license_text_signature(text: str) -> str | None:
    """Узнаём лицензию по полному тексту в поле License (некоторые пакеты кладут туда текст)."""
    t = " ".join(text.split()).lower()
    if "gnu affero general public license" in t:
        return "AGPL-3.0-only"
    if "gnu lesser general public license" in t:
        return "LGPL-2.1-or-later"
    if "gnu general public license" in t:
        return "GPL-2.0-or-later"
    if "permission is hereby granted, free of charge" in t:
        return "MIT"
    if "apache license" in t and "version 2.0" in t:
        return "Apache-2.0"
    if "redistribution and use in source and binary forms" in t:
        return "BSD-3-Clause" if "neither the name" in t else "BSD-2-Clause"
    return None


def _meta_field(meta: md.PackageMetadata, key: str) -> str | None:
    values = meta.get_all(key)
    return str(values[0]) if values else None


def declared_python_licenses(meta: md.PackageMetadata) -> list[str]:
    expression = _meta_field(meta, "License-Expression")
    if expression:
        return [expression]
    declared: list[str] = []
    field = (_meta_field(meta, "License") or "").strip()
    is_short = bool(field) and "\n" not in field and len(field) <= 100
    if is_short and field.upper() not in {"UNKNOWN", "OTHER", "OTHER/PROPRIETARY LICENSE"}:
        declared.append(field)
    for classifier in meta.get_all("Classifier") or []:
        if not classifier.startswith(_CLASSIFIER_PREFIX):
            continue
        name = classifier.rsplit(" :: ", 1)[-1].strip()
        if name in {"OSI Approved", "Other/Proprietary License"}:
            continue
        declared.append(name)
    if not declared and field:
        signature = _license_text_signature(field)
        if signature:
            declared.append(signature)
    return declared


def check_python(policy: GatePolicy, paths: Sequence[str] | None = None) -> list[Decision]:
    dists: Iterable[md.Distribution] = (
        md.distributions(path=list(paths)) if paths else md.distributions()
    )
    seen: set[str] = set()
    decisions: list[Decision] = []
    for dist in dists:
        name = dist.metadata["Name"]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        if (
            name.lower().startswith(policy.python_first_party_prefixes)
            or name.lower() == "qoima-vision"
        ):
            continue
        decisions.append(
            decide(policy, "python", name, dist.version, declared_python_licenses(dist.metadata))
        )
    return sorted(decisions, key=lambda d: d.package.lower())


# ---------------------------------------------------------------- npm


def _npm_declared(entry: dict[str, Any]) -> list[str]:
    lic = entry.get("license")
    if isinstance(lic, str) and lic.strip():
        if lic.upper().startswith("SEE LICENSE IN") or lic.upper() == "UNLICENSED":
            return []
        return [lic]
    if isinstance(lic, dict) and isinstance(lic.get("type"), str):
        return [lic["type"]]
    legacy = entry.get("licenses")
    if isinstance(legacy, list):
        types = [
            str(x["type"]) for x in legacy if isinstance(x, dict) and isinstance(x.get("type"), str)
        ]
        if types:
            return [" OR ".join(types)]
    return []


def check_npm(policy: GatePolicy, lockfile: Path) -> list[Decision]:
    data = json.loads(lockfile.read_text(encoding="utf-8"))
    packages: dict[str, dict[str, Any]] = data.get("packages") or {}
    if not packages:
        raise ValueError(f"{lockfile}: нет раздела packages (нужен lockfileVersion ≥ 2)")
    decisions: list[Decision] = []
    for key, entry in packages.items():
        if key == "" or entry.get("link"):
            continue  # корневой пакет и workspace-ссылки
        name = entry.get("name") or key.rsplit("node_modules/", 1)[-1]
        if name in policy.npm_first_party_names:
            continue
        decisions.append(
            decide(policy, "npm", name, str(entry.get("version", "?")), _npm_declared(entry))
        )
    return sorted(decisions, key=lambda d: (d.package.lower(), d.version))


# ---------------------------------------------------------------- вывод


def _blocking(decisions: Iterable[Decision]) -> list[Decision]:
    return [d for d in decisions if d.verdict in (Verdict.DENIED, Verdict.UNKNOWN)]


def render_text(decisions: list[Decision]) -> str:
    lines = []
    for d in decisions:
        mark = {"allowed": "ok ", "reviewed": "rev", "denied": "DENY", "unknown": "UNKN"}[
            d.verdict.value
        ]
        lines.append(
            f"[{mark:4}] {d.ecosystem}:{d.package}=={d.version}  {d.expression or d.declared}  — {d.reason}"
        )
    blocking = _blocking(decisions)
    lines.append("")
    lines.append(
        f"Всего: {len(decisions)}; разрешено: {sum(d.verdict is Verdict.ALLOWED for d in decisions)}; "
        f"после ревью: {sum(d.verdict is Verdict.REVIEWED for d in decisions)}; блокирует: {len(blocking)}"
    )
    return "\n".join(lines)


def render_markdown(decisions: list[Decision]) -> str:
    lines = ["| Экосистема | Пакет | Версия | Лицензия | Решение |", "|---|---|---|---|---|"]
    for d in decisions:
        lines.append(
            f"| {d.ecosystem} | {d.package} | {d.version} | {d.expression or d.declared} | {d.verdict.value} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qoima-license-gate", description="Лицензионный гейт Qoima Vision"
    )
    parser.add_argument("ecosystem", choices=["python", "npm"])
    parser.add_argument("lockfile", nargs="?", help="для npm: путь к package-lock.json")
    parser.add_argument(
        "--path", action="append", help="для python: каталог site-packages (можно несколько)"
    )
    parser.add_argument("--policy", type=Path, help="альтернативный policy.yaml (для тестов)")
    parser.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    args = parser.parse_args(argv)

    policy = load_policy(args.policy)
    if args.ecosystem == "python":
        decisions = check_python(policy, args.path)
    else:
        if not args.lockfile:
            parser.error("для npm укажите путь к package-lock.json")
        decisions = check_npm(policy, Path(args.lockfile))

    if args.format == "json":
        print(json.dumps([asdict(d) for d in decisions], ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(render_markdown(decisions))
    else:
        print(render_text(decisions))
    blocking = _blocking(decisions)
    if blocking:
        print(
            "\nЛицензионный гейт НЕ пройден: "
            + ", ".join(f"{d.package} ({d.expression or d.declared})" for d in blocking),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
