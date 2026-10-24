"""`qoima doctor` и выбор compose-профиля: проверки на записанном выводе команд, без железа."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from cli.compose import ComposeError, ComposePlan, build_plan, count_gpus, gpu_profile_for, parse_ps
from cli.doctor import (
    Level,
    check_containers,
    check_docker,
    check_nvidia,
    check_nvidia_runtime,
    check_os,
    check_secrets,
    check_time,
    parse_chrony,
    version_at_least,
)
from cli.paths import REQUIRED_SECRETS
from cli.runner import NOT_FOUND, CommandResult, Runner
from shared.config import QoimaConfig

NVIDIA_SMI_HEADER = """
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 580.95.05              Driver Version: 580.95.05      CUDA Version: 13.0     |
+-----------------------------------------+------------------------+----------------------+
"""


def fake_runner(responses: dict[str, CommandResult]) -> Runner:
    """Ответ выбирается по первому совпавшему префиксу команды."""

    def run(args: Sequence[str]) -> CommandResult:
        # Первый аргумент — по имени: nvidia-smi может прийти полным путём (/usr/lib/wsl/lib/...).
        joined = " ".join([Path(args[0]).name, *args[1:]])
        for prefix, result in responses.items():
            if joined.startswith(prefix):
                return result
        return CommandResult(NOT_FOUND, "", f"{args[0]}: команда не найдена")

    return run


def ok(stdout: str) -> CommandResult:
    return CommandResult(0, stdout, "")


GPU_QUERY = "nvidia-smi --query-gpu=index,name,driver_version,memory.total"


def test_nvidia_ok(config: QoimaConfig) -> None:
    runner = fake_runner(
        {
            GPU_QUERY: ok("0, NVIDIA L40S, 580.95.05, 46068\n"),
            "nvidia-smi": ok(NVIDIA_SMI_HEADER),
        }
    )
    outcomes, gpus = check_nvidia(runner, config)
    assert [o.level for o in outcomes] == [Level.OK, Level.OK, Level.OK]
    assert gpus[0].vram_mb == 46068


def test_nvidia_missing_driver(config: QoimaConfig) -> None:
    outcomes, gpus = check_nvidia(fake_runner({}), config)
    assert outcomes[0].level is Level.FAIL and gpus == []
    assert "драйвер" in outcomes[0].recommendation.lower()


@pytest.mark.parametrize(
    "cuda, expected", [("13.3", Level.OK), ("12.8", Level.FAIL), ("N/A", Level.WARN)]
)
def test_nvidia_wsl_umd_header(
    config: QoimaConfig, cuda: str, expected: Level, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Реальный формат WSL / драйвер 610.88; старый CUDA Version проверяется выше.
    monkeypatch.setattr("cli.doctor.nvidia_smi", lambda: "nvidia-smi")
    runner = fake_runner(
        {
            GPU_QUERY: ok("0, NVIDIA GeForce RTX 5070 Ti, 610.88, 16303\n"),
            "nvidia-smi": ok(
                f"| NVIDIA-SMI 610.57.01 KMD Version: 610.88 CUDA UMD Version: {cuda} |"
            ),
        }
    )
    outcomes, _ = check_nvidia(runner, config)
    assert next(o.level for o in outcomes if o.name == "CUDA (драйвер)") is expected


def test_old_cuda_and_small_vram(config: QoimaConfig) -> None:
    runner = fake_runner(
        {
            GPU_QUERY: ok("0, NVIDIA RTX A4000, 550.54, 16376\n"),
            "nvidia-smi": ok(NVIDIA_SMI_HEADER.replace("13.0", "12.4")),
        }
    )
    outcomes, _ = check_nvidia(runner, config)
    levels = {o.name: o.level for o in outcomes}
    assert levels["CUDA (драйвер)"] is Level.FAIL
    assert levels["GPU0"] is Level.WARN  # 16 ГБ < 24 ГБ: режим без локального VLM


def test_docker_checks(config: QoimaConfig) -> None:
    runner = fake_runner(
        {
            "docker version": ok("27.3.1\n"),
            "docker compose version": ok("2.29.7\n"),
        }
    )
    assert [o.level for o in check_docker(runner, config)] == [Level.OK, Level.OK]
    old = fake_runner({"docker version": ok("20.10.0\n"), "docker compose version": ok("2.10.0\n")})
    assert [o.level for o in check_docker(old, config)] == [Level.FAIL, Level.FAIL]


def test_nvidia_runtime() -> None:
    present = fake_runner(
        {"docker info": ok('{"nvidia":{"path":"nvidia-container-runtime"},"runc":{}}')}
    )
    assert check_nvidia_runtime(present).level is Level.OK
    absent = fake_runner({"docker info": ok('{"runc":{"path":"runc"}}')})
    result = check_nvidia_runtime(absent)
    assert result.level is Level.FAIL and "nvidia-ctk" in result.recommendation


CHRONY = """Reference ID    : C0A80001 (ntp.local)
Stratum         : 3
System time     : 0.000012345 seconds fast of NTP time
Leap status     : Normal
"""


def test_chrony_parsing_and_time_check() -> None:
    offset, leap = parse_chrony(CHRONY)
    assert offset == pytest.approx(0.012345) and leap == "Normal"
    assert check_time(fake_runner({"chronyc": ok(CHRONY)}), 50).level is Level.OK
    drift = CHRONY.replace("0.000012345", "0.250000000")
    assert check_time(fake_runner({"chronyc": ok(drift)}), 50).level is Level.FAIL
    unsynced = CHRONY.replace("Normal", "Not synchronised")
    assert check_time(fake_runner({"chronyc": ok(unsynced)}), 50).level is Level.FAIL
    assert check_time(fake_runner({"timedatectl": ok("yes\n")}), 50).level is Level.OK
    assert check_time(fake_runner({}), 50).level is Level.WARN


def test_os_check() -> None:
    release = 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n'
    assert check_os("Linux", release).detail == "Ubuntu 24.04.1 LTS"
    assert check_os("Darwin", None).level is Level.WARN


def test_secrets_check(tmp_path: Path) -> None:
    d = tmp_path / ".secrets"
    assert check_secrets(d).level is Level.FAIL
    d.mkdir(mode=0o700)
    for name in REQUIRED_SECRETS:
        (d / name).write_text("x")
    d.chmod(0o700)
    assert check_secrets(d).level is Level.OK
    d.chmod(0o755)
    assert check_secrets(d).level is Level.FAIL


def test_version_compare() -> None:
    assert version_at_least("27.3.1", "24.0")
    assert version_at_least("13.0", "13.0")
    assert not version_at_least("12.8", "13.0")
    assert version_at_least("v2.29.7-desktop.1", "2.20")


# ---------------------------------------------------------------- compose


def test_gpu_profile_selection() -> None:
    assert gpu_profile_for(1, allow_no_gpu=False) == "gpu1"
    assert gpu_profile_for(2, allow_no_gpu=False) == "gpu2"
    assert gpu_profile_for(4, allow_no_gpu=False) == "gpu2"
    assert gpu_profile_for(0, allow_no_gpu=True) == "none"
    with pytest.raises(ComposeError, match="--no-gpu"):
        gpu_profile_for(0, allow_no_gpu=False)


def test_plan_files(repo_root: Path) -> None:
    plan = build_plan(
        repo_root,
        gpu_count=2,
        allow_no_gpu=False,
        engineering=True,
        data_root=Path("/var/lib/qoima"),
        secrets_dir=repo_root / ".secrets",
        https_port=443,
    )
    names = [f.name for f in plan.files]
    assert names == ["compose.yml", "compose.gpu2.yml", "compose.engineering.yml"]
    assert all(f.is_file() for f in plan.files)
    cmd = plan.command("up", "--wait")
    assert cmd[:4] == ["docker", "compose", "--project-name", "qoima"] and cmd[-2:] == [
        "up",
        "--wait",
    ]
    assert plan.env["QOIMA_DATA_ROOT"] == "/var/lib/qoima"


def test_count_gpus() -> None:
    assert count_gpus(fake_runner({"nvidia-smi": ok("0\n1\n")})) == 2
    assert count_gpus(fake_runner({})) == 0


def test_parse_ps_formats() -> None:
    ndjson = '{"Service":"postgres","State":"running","Health":"healthy"}\n{"Service":"ui","State":"exited","Health":""}\n'
    array = '[{"Service":"postgres","State":"running","Health":"healthy"}]'
    assert [s["Service"] for s in parse_ps(ndjson)] == ["postgres", "ui"]
    assert parse_ps(array)[0]["Health"] == "healthy"
    assert parse_ps("") == []


def test_container_check(repo_root: Path) -> None:
    plan = ComposePlan(home=repo_root, gpu_profile="none", engineering=False)
    ps = (
        '{"Service":"postgres","State":"running","Health":"healthy"}\n'
        '{"Service":"core-api","State":"running","Health":"unhealthy"}\n'
        '{"Service":"valkey","State":"exited","Health":""}\n'
    )
    outcomes = {
        o.name: o.level for o in check_containers(fake_runner({"docker compose": ok(ps)}), plan)
    }
    assert outcomes == {
        "сервис core-api": Level.FAIL,
        "сервис postgres": Level.OK,
        "сервис valkey": Level.FAIL,
    }
    empty = check_containers(fake_runner({"docker compose": ok("")}), plan)
    assert empty[0].level is Level.SKIP


# ---------------------------------------------------------------- WSL2 (Windows)


def test_gpu_in_docker_without_nvidia_runtime_docker_desktop() -> None:
    """Docker Desktop даёт GPU без runtime «nvidia» — проверка делом засчитывает это."""
    desktop = fake_runner(
        {
            "docker info": ok('{"runc":{"path":"runc"}}'),
            "docker run --rm --gpus all": ok("GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-1)\n"),
        }
    )
    result = check_nvidia_runtime(desktop)
    assert result.level is Level.OK and "RTX 4090" in result.detail


def test_os_check_wsl() -> None:
    wsl = check_os("Linux", 'PRETTY_NAME="Ubuntu 24.04.1 LTS"', wsl=True)
    assert wsl.level is Level.WARN and "WSL2" in wsl.detail and "Ubuntu" in wsl.recommendation


def test_time_failure_is_warning_on_wsl() -> None:
    drift = CHRONY.replace("0.000012345", "0.250000000")
    assert check_time(fake_runner({"chronyc": ok(drift)}), 50, wsl=True).level is Level.WARN
    assert check_time(fake_runner({"chronyc": ok(drift)}), 50).level is Level.FAIL


def test_wsl_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shutil as sh
    from collections import namedtuple

    from cli import doctor

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(doctor, "windows_drive", lambda letter="c": tmp_path)
    monkeypatch.setattr(
        sh, "disk_usage", lambda p: usage(500 * 1024**3, 450 * 1024**3, 50 * 1024**3)
    )
    meminfo = "MemTotal:        8000000 kB\n"
    nat = doctor.check_wsl(
        fake_runner({"wslinfo --networking-mode": ok("nat\n")}), 200, 16, meminfo
    )
    by = {o.name: o for o in nat}
    assert by["WSL: сеть"].level is Level.WARN and "mirrored" in by["WSL: сеть"].recommendation
    assert by["WSL: диск Windows"].level is Level.FAIL  # 50 ГБ на C: < 200 ГБ
    assert (
        by["WSL: память"].level is Level.WARN and ".wslconfig" in by["WSL: память"].recommendation
    )
    mirrored = doctor.check_wsl(
        fake_runner({"wslinfo --networking-mode": ok("mirrored\n")}), 20, 4, meminfo
    )
    by = {o.name: o for o in mirrored}
    assert by["WSL: сеть"].level is Level.OK and by["WSL: диск Windows"].level is Level.OK
    assert by["WSL: память"].level is Level.OK


def test_hostenv_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from cli import hostenv

    assert hostenv.is_wsl("5.15.167.4-microsoft-standard-WSL2", tmp_path / "none")
    assert not hostenv.is_wsl("6.8.0-45-generic", tmp_path / "none")
    assert hostenv.under_windows_mount(Path("/mnt/c/Users/me/qoima"))
    assert not hostenv.under_windows_mount(Path("/home/me/qoima"))
    fake_smi = tmp_path / "nvidia-smi"
    fake_smi.write_text("")
    monkeypatch.setattr(hostenv.shutil, "which", lambda name: None)
    monkeypatch.setattr(hostenv, "WSL_NVIDIA_SMI", fake_smi)
    assert hostenv.nvidia_smi() == str(fake_smi)  # под sudo/systemd PATH без /usr/lib/wsl/lib
    monkeypatch.setattr(hostenv, "WSL_NVIDIA_SMI", tmp_path / "missing")
    assert hostenv.nvidia_smi() == "nvidia-smi"
    assert hostenv.wsl_networking_mode(fake_runner({})) is None  # старый WSL без wslinfo
