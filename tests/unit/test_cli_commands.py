"""Команды CLI: config validate и честные заглушки команд следующих фаз."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import main as cli_main

runner = CliRunner()


def test_config_validate(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QOIMA_HOME", str(repo_root))
    result = runner.invoke(cli_main.app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "Конфигурация корректна" in result.output


@pytest.mark.parametrize(
    ("args", "phase"),
    [(["bench"], 3), (["eval"], 5), (["backup"], 9), (["update", "x.tar"], 9)],
)
def test_future_commands_report_phase(args: list[str], phase: int) -> None:
    result = runner.invoke(cli_main.app, args)
    assert isinstance(result.exception, NotImplementedError)
    assert f"Фазе {phase}" in str(result.exception)


def test_main_converts_not_implemented_to_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["qoima", "bench"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 2


def test_sim_fault_validates_kind() -> None:
    result = runner.invoke(cli_main.app, ["sim", "fault", "1", "explode"])
    assert result.exit_code == 1
    assert "неизвестная неисправность" in result.output
