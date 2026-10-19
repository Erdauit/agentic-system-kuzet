"""ORM-модели и контракты не расходятся: CHECK-ограничения перечислений = значения enum."""

from __future__ import annotations

import re
from enum import StrEnum

import pytest
from sqlalchemy import CheckConstraint

from core_api.db.models import Base
from shared.schemas import (
    SAFETY_EVENT_TYPES,
    CameraSource,
    EventType,
    FeedbackVerdict,
    IncidentState,
    ModelSource,
    ModelStatus,
    PermissionLevel,
    ReasonCode,
    Role,
    RoutingDecision,
    Severity,
    TaskType,
    ToolCallStatus,
    ZoneKind,
)

EXPECTED: dict[tuple[str, str], set[str]] = {
    ("users", "role"): {e.value for e in Role},
    ("audit_log", "permission"): {e.value for e in PermissionLevel},
    ("audit_log", "status"): {e.value for e in ToolCallStatus},
    ("cameras", "source"): {e.value for e in CameraSource},
    ("zones", "kind"): {e.value for e in ZoneKind},
    ("incidents", "type"): {e.value for e in SAFETY_EVENT_TYPES},
    ("incidents", "state"): {e.value for e in IncidentState},
    ("incidents", "severity"): {e.value for e in Severity},
    ("events", "type"): {e.value for e in EventType},
    ("events", "routing"): {e.value for e in RoutingDecision},
    ("feedback", "verdict"): {e.value for e in FeedbackVerdict},
    ("feedback", "reason_code"): {e.value for e in ReasonCode},
    ("models", "task_type"): {e.value for e in TaskType},
    ("models", "source"): {e.value for e in ModelSource},
    ("models", "status"): {e.value for e in ModelStatus},
}


def _enum_values(table: str, column: str) -> set[str]:
    t = Base.metadata.tables[table]
    for c in t.constraints:
        if isinstance(c, CheckConstraint) and str(c.name).endswith(f"{column}_valid"):
            text = str(c.sqltext)
            m = re.search(rf"{column} IN \(([^)]*)\)", text)
            assert m, text
            return set(re.findall(r"'([^']*)'", m.group(1)))
    raise AssertionError(f"нет CHECK для {table}.{column}")


@pytest.mark.parametrize(("table", "column"), sorted(EXPECTED))
def test_check_constraints_match_contracts(table: str, column: str) -> None:
    assert _enum_values(table, column) == EXPECTED[(table, column)]


def test_all_enum_checks_covered() -> None:
    covered = {f"{t}.{c}" for t, c in EXPECTED}
    found = set()
    for table in Base.metadata.tables.values():
        for c in table.constraints:
            if (
                isinstance(c, CheckConstraint)
                and str(c.name).endswith("_valid")
                and " IN (" in str(c.sqltext)
            ):
                col = str(c.name).removeprefix(f"ck_{table.name}_").removesuffix("_valid")
                found.add(f"{table.name}.{col}")
    assert found - {"audit_log.kind"} == covered


def test_str_enums_used() -> None:
    assert issubclass(Role, StrEnum)
