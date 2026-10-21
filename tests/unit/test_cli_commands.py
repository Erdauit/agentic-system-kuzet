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
    [(["eval"], 5), (["backup"], 9), (["update", "x.tar"], 9)],
)
def test_future_commands_report_phase(args: list[str], phase: int) -> None:
    result = runner.invoke(cli_main.app, args)
    assert isinstance(result.exception, NotImplementedError)
    assert f"Фазе {phase}" in str(result.exception)


def test_main_converts_not_implemented_to_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["qoima", "eval"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 2


def test_sim_fault_validates_kind() -> None:
    result = runner.invoke(cli_main.app, ["sim", "fault", "1", "explode"])
    assert result.exit_code == 1
    assert "неизвестная неисправность" in result.output


def test_bench_runs_one_off_pipeline_container_and_prints_summary(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`qoima bench` — разовый контейнер vision-pipeline; сводка читается из каталога данных."""
    import json
    import subprocess

    from tests.contract.examples import EXAMPLES

    monkeypatch.setenv("QOIMA_HOME", str(repo_root))
    monkeypatch.setenv("QOIMA_DATA_ROOT", str(tmp_path))
    calls: list[list[str]] = []

    real_run = subprocess.run

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if "vision_pipeline.bench" not in cmd:  # ip/ifconfig и прочие вызовы CLI — по-настоящему
            return real_run(cmd, **kw)  # type: ignore[call-overload,no-any-return]
        calls.append(cmd)
        out = tmp_path / "bench"
        out.mkdir(exist_ok=True)
        (out / "bench-20261019-080000.json").write_text(json.dumps(EXAMPLES["BenchResult"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)
    monkeypatch.setattr(cli_main, "count_gpus", lambda runner: 0)
    result = runner.invoke(
        cli_main.app, ["bench", "--no-gpu", "--vlm-url", "http://llm:8000", "--quick"]
    )
    assert result.exit_code == 0, result.output
    cmd = calls[0]
    i = cmd.index("run")
    assert cmd[i : i + 5] == ["run", "--rm", "--no-deps", "-T", "vision-pipeline"]
    tail = ["--out", "/var/lib/qoima/bench", "--vlm-url", "http://llm:8000", "--quick"]
    assert cmd[-len(tail) :] == tail
    assert "bench-20261019-080000" in result.output and "NVIDIA L40S" in result.output
