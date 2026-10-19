"""Лицензионная политика и гейт CI (SPEC.md §2.7).

policy.yaml — белый список SPDX, запреты, алиасы и пакеты после ревью.
components.yaml — реестр Docker-образов и их лицензий.
"""

from shared.licenses.gate import Decision, GatePolicy, check_npm, check_python, decide, load_policy
from shared.licenses.spdx import Policy, Verdict, parse, render

__all__ = [
    "Decision",
    "GatePolicy",
    "Policy",
    "Verdict",
    "check_npm",
    "check_python",
    "decide",
    "load_policy",
    "parse",
    "render",
]
