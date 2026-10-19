"""CLI `qoima` (SPEC.md §3.1).

Фаза 0 реализует: up, down, status, doctor, config validate. Остальные команды существуют,
чтобы пользователь видел полный набор, но честно сообщают, в какой фазе появятся.
"""

from __future__ import annotations

import json
import os
import platform
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from cli import __version__
from cli.compose import ComposeError, ComposePlan, build_plan, count_gpus, parse_ps
from cli.doctor import (
    Level,
    Outcome,
    check_config,
    check_containers,
    check_disk,
    check_docker,
    check_nvidia,
    check_nvidia_runtime,
    check_os,
    check_secrets,
    check_time,
)
from cli.paths import DATA_DIRS, HomeNotFoundError, data_root, qoima_home, secrets_dir
from cli.runner import Runner, run_command
from shared.config import ConfigError, QoimaConfig, load_config

app = typer.Typer(
    name="qoima",
    help="Qoima Vision — управление системой видеоаналитики на сервере предприятия.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Работа с конфигурацией (configs/).", no_args_is_help=True)
app.add_typer(config_app, name="config")

console = Console()
err_console = Console(stderr=True)

_LEVEL_STYLE = {Level.OK: "green", Level.WARN: "yellow", Level.FAIL: "bold red", Level.SKIP: "dim"}
_LEVEL_TEXT = {
    Level.OK: "OK",
    Level.WARN: "ВНИМАНИЕ",
    Level.FAIL: "ОШИБКА",
    Level.SKIP: "пропущено",
}

NoGpuOpt = Annotated[
    bool, typer.Option("--no-gpu", help="Режим разработки без GPU (не для объекта)")
]
EngineeringOpt = Annotated[
    bool, typer.Option("--engineering", help="Добавить профиль engineering (Grafana)")
]
ConfigDirOpt = Annotated[
    Path | None, typer.Option("--config-dir", help="Каталог конфигов (по умолчанию configs/)")
]


def _fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[bold red]Ошибка:[/] {message}")
    raise typer.Exit(code)


def _home() -> Path:
    try:
        return qoima_home()
    except HomeNotFoundError as exc:
        _fail(str(exc))


def _load(config_dir: Path | None, home: Path) -> QoimaConfig:
    try:
        return load_config(config_dir or home / "configs")
    except ConfigError as exc:
        _fail("конфигурация не прошла проверку:\n  " + "\n  ".join(exc.errors))


def _https_port() -> int:
    return int(os.environ.get("QOIMA_HTTPS_PORT", "443"))


def _plan(
    cfg: QoimaConfig, home: Path, runner: Runner, *, no_gpu: bool, engineering: bool
) -> ComposePlan:
    gpus = 0 if no_gpu else count_gpus(runner)
    try:
        return build_plan(
            home,
            gpu_count=gpus,
            allow_no_gpu=no_gpu,
            engineering=engineering,
            data_root=data_root(cfg.defaults.storage.data_root),
            secrets_dir=secrets_dir(home),
            https_port=_https_port(),
        )
    except ComposeError as exc:
        _fail(str(exc))


def _render_outcomes(title: str, outcomes: list[Outcome]) -> None:
    table = Table(title=title, show_lines=False)
    table.add_column("Проверка")
    table.add_column("Статус")
    table.add_column("Детали")
    table.add_column("Рекомендация")
    for o in outcomes:
        style = _LEVEL_STYLE[o.level]
        table.add_row(o.name, f"[{style}]{_LEVEL_TEXT[o.level]}[/]", o.detail, o.recommendation)
    console.print(table)


def _compose(plan: ComposePlan, *args: str, timeout_s: float = 900) -> int:
    cmd = plan.command(*args)
    result = run_command(cmd, timeout_s=timeout_s, env=plan.process_env())
    if result.stdout.strip():
        console.print(result.stdout.rstrip())
    if not result.ok and result.stderr.strip():
        err_console.print(result.stderr.rstrip())
    return result.returncode


@app.command()
def version() -> None:
    """Версия CLI."""
    console.print(f"qoima {__version__}")


@app.command()
def up(
    no_gpu: NoGpuOpt = False,
    engineering: EngineeringOpt = False,
    build: Annotated[
        bool, typer.Option("--build", help="Пересобрать образы Qoima перед запуском")
    ] = False,
    wait_timeout: Annotated[
        int, typer.Option(help="Сколько секунд ждать healthy всех сервисов")
    ] = 600,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Поднять систему и дождаться, пока все сервисы станут healthy."""
    home = _home()
    cfg = _load(config_dir, home)
    secret_check = check_secrets(secrets_dir(home))
    if secret_check.level is Level.FAIL:
        _fail(f"{secret_check.detail}. {secret_check.recommendation}")
    plan = _plan(cfg, home, run_command, no_gpu=no_gpu, engineering=engineering)
    root = Path(plan.env["QOIMA_DATA_ROOT"])
    for sub in DATA_DIRS:
        try:
            (root / sub).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            _fail(
                f"нет прав на {root / sub}. Запустите ./install.sh от root — он создаст каталоги данных"
            )
    if plan.gpu_profile == "none":
        err_console.print("[yellow]Режим разработки без GPU: Triton и DCGM не запускаются.[/]")
    console.print(f"Запуск ({plan.gpu_profile}{', engineering' if engineering else ''})…")
    args = ["up", "--detach", "--wait", "--wait-timeout", str(wait_timeout), "--remove-orphans"]
    if build:
        args.append("--build")
    code = _compose(plan, *args, timeout_s=wait_timeout + 600)
    if code != 0:
        _fail("не все сервисы стали healthy. Подробности: qoima status, qoima doctor", code)
    console.print(f"[green]Готово.[/] Откройте https://<адрес-сервера>:{_https_port()}/")


@app.command()
def down(
    no_gpu: NoGpuOpt = False, engineering: EngineeringOpt = False, config_dir: ConfigDirOpt = None
) -> None:
    """Остановить систему. Данные (БД, клипы, модели) не удаляются."""
    home = _home()
    cfg = _load(config_dir, home)
    plan = _plan(cfg, home, run_command, no_gpu=no_gpu, engineering=engineering)
    raise typer.Exit(_compose(plan, "down", "--remove-orphans"))


def fetch_health(port: int, ca_file: Path) -> dict[str, object] | None:
    """Сводка core-api по HTTPS с проверкой сертификата из .secrets/tls.crt."""
    context = ssl.create_default_context(cafile=str(ca_file)) if ca_file.is_file() else None
    url = f"https://localhost:{port}/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=10, context=context) as resp:
            data: dict[str, object] = json.loads(resp.read())
            return data
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return None


@app.command()
def status(
    no_gpu: NoGpuOpt = False, engineering: EngineeringOpt = False, config_dir: ConfigDirOpt = None
) -> None:
    """Статус контейнеров и сводка «Состояние системы» из core-api."""
    home = _home()
    cfg = _load(config_dir, home)
    plan = _plan(cfg, home, run_command, no_gpu=no_gpu, engineering=engineering)
    ps = run_command(plan.command("ps", "--all", "--format", "json"), env=plan.process_env())
    if not ps.ok:
        _fail(ps.stderr.strip() or "docker compose ps завершился с ошибкой")
    services = parse_ps(ps.stdout)
    if not services:
        console.print("Система не запущена. Запустите: [bold]qoima up[/]")
        raise typer.Exit(1)
    table = Table(title="Контейнеры")
    for col in ("Сервис", "Состояние", "Health"):
        table.add_column(col)
    for svc in sorted(services, key=lambda s: str(s.get("Service"))):
        health = str(svc.get("Health") or "—")
        style = "green" if health in ("healthy", "—") and svc.get("State") == "running" else "red"
        table.add_row(str(svc.get("Service")), str(svc.get("State")), f"[{style}]{health}[/]")
    console.print(table)

    summary = fetch_health(_https_port(), secrets_dir(home) / "tls.crt")
    if summary is None:
        console.print("[red]core-api недоступен по HTTPS — сводка состояния не получена[/]")
        raise typer.Exit(1)
    comp_table = Table(title=f"Состояние системы: {summary.get('status')}")
    for col in ("Компонент", "Статус", "Сообщение"):
        comp_table.add_column(col)
    components = summary.get("components")
    for c in components if isinstance(components, list) else []:
        comp_table.add_row(str(c.get("component")), str(c.get("status")), str(c.get("message")))
    console.print(comp_table)


@app.command()
def doctor(
    no_gpu: NoGpuOpt = False,
    engineering: EngineeringOpt = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Полная самодиагностика: драйвер, CUDA, Docker, toolkit, диск, время, секреты, контейнеры."""
    home = _home()
    runner: Runner = run_command
    outcomes: list[Outcome] = []

    os_release_path = Path("/etc/os-release")
    outcomes.append(
        check_os(
            platform.system(), os_release_path.read_text() if os_release_path.is_file() else None
        )
    )
    config_outcome, cfg = check_config(config_dir or home / "configs")
    outcomes.append(config_outcome)
    if cfg is None:
        _render_outcomes("qoima doctor", outcomes)
        raise typer.Exit(1)

    if no_gpu:
        outcomes.append(Outcome("Драйвер NVIDIA", Level.SKIP, "режим разработки --no-gpu"))
    else:
        nvidia_outcomes, _gpus = check_nvidia(runner, cfg)
        outcomes.extend(nvidia_outcomes)
    outcomes.extend(check_docker(runner, cfg))
    if not no_gpu:
        outcomes.append(check_nvidia_runtime(runner))
    outcomes.append(
        check_disk(data_root(cfg.defaults.storage.data_root), cfg.defaults.doctor.min_free_disk_gb)
    )
    outcomes.append(check_time(runner, cfg.defaults.doctor.max_clock_offset_ms))
    outcomes.append(check_secrets(secrets_dir(home)))
    try:
        plan = build_plan(
            home,
            gpu_count=0 if no_gpu else count_gpus(runner),
            allow_no_gpu=True,
            engineering=engineering,
            data_root=data_root(cfg.defaults.storage.data_root),
            secrets_dir=secrets_dir(home),
            https_port=_https_port(),
        )
        outcomes.extend(check_containers(runner, plan))
    except ComposeError as exc:
        outcomes.append(Outcome("Контейнеры", Level.SKIP, str(exc)))

    _render_outcomes("qoima doctor", outcomes)
    failed = [o for o in outcomes if o.level is Level.FAIL]
    if failed:
        err_console.print(
            f"[bold red]Проблем: {len(failed)}.[/] Исправьте пункты с рекомендациями и повторите."
        )
        raise typer.Exit(1)
    console.print("[green]Блокирующих проблем не найдено.[/]")


@config_app.command("validate")
def config_validate(config_dir: ConfigDirOpt = None) -> None:
    """Проверить configs/*.yaml и site.yaml."""
    cfg = _load(config_dir, _home())
    console.print(
        f"[green]Конфигурация корректна.[/] Объект: «{cfg.defaults.site.name}», "
        f"сценарии: {', '.join(sorted(s.value for s in cfg.scenarios))}"
    )


# ---------------------------------------------------------------- команды следующих фаз


def _later(command: str, phase: int) -> NoReturn:
    raise NotImplementedError(f"`qoima {command}` реализуется в Фазе {phase} (см. docs/TODO.md)")


@app.command()
def bench() -> None:
    """Бенчмарк NVDEC, моделей и VLM на этом сервере (Фаза 3)."""
    _later("bench", 3)


@app.command("eval")
def eval_() -> None:
    """Регрессионные evals агентов (Фаза 5)."""
    _later("eval", 5)


@app.command()
def sim() -> None:
    """Симулятор камер из samples/ (Фаза 1)."""
    _later("sim", 1)


@app.command()
def update(bundle: Annotated[Path, typer.Argument(help="Подписанный офлайн-бандл")]) -> None:
    """Обновление из офлайн-бандла с автоматическим откатом (Фаза 9)."""
    _later("update", 9)


@app.command()
def backup() -> None:
    """Резервная копия Postgres и конфигурации (Фаза 9)."""
    _later("backup", 9)


@app.command()
def restore() -> None:
    """Восстановление из резервной копии (Фаза 9)."""
    _later("restore", 9)


@app.command("support-bundle")
def support_bundle() -> None:
    """Логи, метрики и конфиг без секретов для инженеров Qoima (Фаза 9)."""
    _later("support-bundle", 9)


def main() -> None:
    try:
        app()
    except NotImplementedError as exc:
        err_console.print(f"[yellow]{exc}[/]")
        sys.exit(2)


if __name__ == "__main__":
    main()
