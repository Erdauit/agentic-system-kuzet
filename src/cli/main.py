"""CLI `qoima` (SPEC.md §3.1).

Фаза 0 реализует: up, down, status, doctor, config validate. Остальные команды существуют,
чтобы пользователь видел полный набор, но честно сообщают, в какой фазе появятся.
"""

from __future__ import annotations

import json
import os
import platform
import re
import ssl
import subprocess
import sys
import time
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
    Gpu,
    Level,
    Outcome,
    check_cameras,
    check_config,
    check_containers,
    check_disk,
    check_docker,
    check_nvidia,
    check_nvidia_runtime,
    check_os,
    check_secrets,
    check_time,
    check_wsl,
    write_report,
)
from cli.hostenv import is_wsl, wsl_networking_mode
from cli.netinfo import host_addresses
from cli.paths import (
    DATA_DIRS,
    RUNTIME_SECRETS,
    HomeNotFoundError,
    data_root,
    qoima_home,
    secrets_dir,
)
from cli.runner import Runner, run_command
from shared.config import ConfigError, QoimaConfig, load_config
from shared.schemas import BenchResult

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
    addresses = host_addresses(runner)
    try:
        return build_plan(
            home,
            gpu_count=gpus,
            allow_no_gpu=no_gpu,
            engineering=engineering,
            data_root=data_root(cfg.defaults.storage.data_root),
            secrets_dir=secrets_dir(home),
            https_port=_https_port(),
            host_subnets=sorted({str(a.network) for a in addresses}),
            host_ips=[str(a.ip) for a in addresses],
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
    (secrets_dir(home) / RUNTIME_SECRETS).mkdir(mode=0o700, exist_ok=True)
    if plan.gpu_profile == "none":
        err_console.print("[yellow]Режим разработки без GPU: Triton и DCGM не запускаются.[/]")
    subnets = plan.env.get("QOIMA_HOST_SUBNETS") or "не найдены"
    console.print(f"Подсети сервера для автопоиска камер: {subnets}")
    if build:
        console.print("Сборка образов (первый раз — долго: FFmpeg собирается из исходников)…")
        if (
            _compose(plan, "--profile", "build", "build", "media-base", timeout_s=3600) != 0
            or _compose(plan, "build", timeout_s=3600) != 0
        ):
            _fail("сборка образов не удалась")
    console.print(f"Запуск ({plan.gpu_profile}{', engineering' if engineering else ''})…")
    args = ["up", "--detach", "--wait", "--wait-timeout", str(wait_timeout)]
    if plan.gpu_profile != "none" and is_wsl():
        # В WSL2 мониторинг DCGM недоступен: без этого up ждал бы healthy dcgm-exporter вечно.
        err_console.print(
            "[yellow]WSL2: мониторинг GPU (dcgm-exporter) не запускается — метрики GPU недоступны.[/]"
        )
        args += ["--scale", "dcgm-exporter=0"]
    if is_wsl() and wsl_networking_mode(run_command) != "mirrored":
        err_console.print(
            "[yellow]WSL2 в режиме NAT: автопоиск увидит только сеть WSL. Включите "
            "networkingMode=mirrored (docs/WINDOWS.md) или впишите подсеть камер в мастере, шаг 1.[/]"
        )
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
    report: Annotated[
        bool,
        typer.Option(
            "--report", help="Записать отчёт для UI (мастер, шаг 2; «Время» в состоянии системы)"
        ),
    ] = False,
) -> None:
    """Полная самодиагностика: драйвер, CUDA, Docker, toolkit, диск, время, секреты, контейнеры, камеры."""
    home = _home()
    runner: Runner = run_command
    outcomes: list[Outcome] = []
    gpus: list[Gpu] = []

    os_release_path = Path("/etc/os-release")
    wsl = is_wsl()
    outcomes.append(
        check_os(
            platform.system(),
            os_release_path.read_text() if os_release_path.is_file() else None,
            wsl,
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
        nvidia_outcomes, gpus = check_nvidia(runner, cfg)
        outcomes.extend(nvidia_outcomes)
    outcomes.extend(check_docker(runner, cfg))
    if not no_gpu:
        outcomes.append(check_nvidia_runtime(runner))
    doc = cfg.defaults.doctor
    min_free = doc.wsl_min_free_disk_gb if wsl else doc.min_free_disk_gb
    outcomes.append(check_disk(data_root(cfg.defaults.storage.data_root), min_free))
    outcomes.append(check_time(runner, cfg.defaults.doctor.max_clock_offset_ms, wsl))
    if wsl:
        meminfo = Path("/proc/meminfo")
        outcomes.extend(
            check_wsl(
                runner,
                doc.wsl_min_free_disk_gb,
                doc.wsl_min_memory_gb,
                meminfo.read_text() if meminfo.is_file() else None,
            )
        )
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
        container_outcomes = check_containers(runner, plan)
        outcomes.extend(container_outcomes)
        if any(o.name == "сервис core-api" and o.level is Level.OK for o in container_outcomes):
            outcomes.extend(check_cameras(runner, plan))
    except ComposeError as exc:
        outcomes.append(Outcome("Контейнеры", Level.SKIP, str(exc)))

    _render_outcomes("qoima doctor", outcomes)
    if report:
        path = write_report(data_root(cfg.defaults.storage.data_root), outcomes, gpus, __version__)
        console.print(f"Отчёт записан: {path}")
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


BENCH_DIR_IN_CONTAINER = "/var/lib/qoima/bench"


@app.command()
def bench(
    no_gpu: NoGpuOpt = False,
    vlm_url: Annotated[
        str | None,
        typer.Option(
            "--vlm-url",
            help="OpenAI-совместимый сервер VLM (vLLM/SGLang), например http://llm:8000",
        ),
    ] = None,
    vlm_model: Annotated[
        str | None, typer.Option("--vlm-model", help="Модель на сервере VLM")
    ] = None,
    no_cameras: Annotated[
        bool, typer.Option("--no-cameras", help="Мерить только синтетические образцы видео")
    ] = False,
    quick: Annotated[
        bool, typer.Option("--quick", help="Короткий прогон (проверка, не для плана)")
    ] = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Бенчмарк этого сервера: декодирование (NVDEC/CPU), модели, VLM → основа плана мощности.

    Запускается разовым контейнером из образа vision-pipeline — в той же среде, что и рабочий
    пайплайн. Распознавание при этом не останавливается. Результат — в каталоге данных bench/,
    план мощности по нему — в мастере настройки, шаг 6.
    """
    home = _home()
    cfg = _load(config_dir, home)
    plan = _plan(cfg, home, run_command, no_gpu=no_gpu, engineering=False)
    host_dir = Path(plan.env["QOIMA_DATA_ROOT"]) / "bench"
    args = ["python", "-m", "vision_pipeline.bench", "--out", BENCH_DIR_IN_CONTAINER]
    if vlm_url:
        args += ["--vlm-url", vlm_url]
    if vlm_model:
        args += ["--vlm-model", vlm_model]
    if no_cameras:
        args.append("--no-cameras")
    if quick:
        args.append("--quick")
    started = time.time()
    console.print("Бенчмарк запущен: несколько минут, распознавание продолжает работать…")
    cmd = plan.command("run", "--rm", "--no-deps", "-T", "vision-pipeline", *args)
    code = subprocess.run(cmd, env=plan.process_env(), check=False).returncode
    if code != 0:
        _fail(f"бенчмарк завершился с ошибкой (код {code}); логи выше")
    results = sorted(
        (f for f in host_dir.glob("bench-*.json") if f.stat().st_mtime >= started - 1),
        key=lambda f: f.stat().st_mtime,
    )
    if not results:
        _fail(f"бенчмарк не записал результат в {host_dir}")
    result = BenchResult.model_validate_json(results[-1].read_text())
    _print_bench(result)
    console.print(f"Результат: {results[-1]}. План мощности: мастер настройки → шаг 6.")


def _print_bench(result: BenchResult) -> None:
    hw = result.hardware
    gpus = ", ".join(f"{g.name} {g.vram_total_mb // 1024} ГБ" for g in hw.gpus) or "нет GPU"
    console.print(f"[bold]{result.id}[/]: {hw.cpu_model} ({hw.cpu_cores} ядер), {gpus}")
    table = Table(title="Декодирование + миниатюра движения")
    for col in ("Поток", "Где", "Потоков", "Кадров/с", "Разброс"):
        table.add_column(col)
    for m in result.decode:
        table.add_row(
            f"{m.codec} {m.width}×{m.height}",
            m.backend.value,
            str(m.parallel_streams),
            f"{m.fps_total:.0f}",
            f"{m.cv:.1%}",
        )
    console.print(table)
    if result.models:
        mt = Table(title="Модели")
        for col in ("Модель", "Батч", "p50, мс", "p95, мс", "Изобр/с", "Провайдер"):
            mt.add_column(col)
        for mm in result.models:
            mt.add_row(
                mm.model,
                str(mm.batch),
                f"{mm.latency_p50_ms:.1f}",
                f"{mm.latency_p95_ms:.1f}",
                f"{mm.throughput:.0f}",
                mm.providers[0],
            )
        console.print(mt)
    for v in result.vlm:
        console.print(
            f"VLM {v.server} {v.model}: {v.concurrency} одновр. → {v.rps:.2f} запр/с, "
            f"p95 {v.latency_p95_s:.1f} с, валидный JSON {v.valid_json_ratio:.0%}"
        )
    for w in result.warnings:
        console.print(f"[yellow]•[/] {w}")


@app.command("eval")
def eval_() -> None:
    """Регрессионные evals агентов (Фаза 5)."""
    _later("eval", 5)


sim_app = typer.Typer(
    help="Симулятор IP-камер для разработки и проверки (RTSP + ONVIF).", no_args_is_help=True
)
app.add_typer(sim_app, name="sim")
user_app = typer.Typer(help="Пользователи (сброс пароля и т.п.).", no_args_is_help=True)
app.add_typer(user_app, name="user")

SIM_FAULTS = ("none", "freeze", "blur", "black", "overexposed", "occlude", "shift", "stop")


def _sim_plan(no_gpu: bool, engineering: bool, config_dir: Path | None) -> ComposePlan:
    home = _home()
    return _plan(
        _load(config_dir, home), home, run_command, no_gpu=no_gpu, engineering=engineering
    ).with_sim()


@sim_app.command("up")
def sim_up(
    cameras: Annotated[
        int, typer.Option("--cameras", min=1, max=32, help="Сколько камер поднять")
    ] = 4,
    no_gpu: NoGpuOpt = False,
    engineering: EngineeringOpt = False,
    build: Annotated[bool, typer.Option("--build", help="Пересобрать образ симулятора")] = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Поднять N симулированных камер в сети edge (их найдёт «Найти камеры» в мастере)."""
    plan = _sim_plan(no_gpu, engineering, config_dir)
    if (
        build
        and _compose(
            plan, "--profile", "build", "build", "media-base", "camera-sim", timeout_s=3600
        )
        != 0
    ):
        _fail("сборка симулятора не удалась")
    args = [
        "up",
        "--detach",
        "--wait",
        "--no-deps",
        "--scale",
        f"camera-sim={cameras}",
        "camera-sim",
        "alert-sink",
    ]
    if _compose(plan, *args, timeout_s=900) != 0:
        _fail("симулятор не запустился")
    console.print(
        f"[green]Симулятор: {cameras} камер.[/] Логин/пароль: admin / qoima-sim. "
        "Мастер → Камеры → «Найти камеры». Неисправность: qoima sim fault <N> freeze|blur|stop|…; "
        "ролик: qoima sim play <N> e2e/fall_staged.mp4; приёмник алертов: http://alert-sink:8080"
    )


@sim_app.command("down")
def sim_down(
    no_gpu: NoGpuOpt = False, engineering: EngineeringOpt = False, config_dir: ConfigDirOpt = None
) -> None:
    """Остановить симулятор камер."""
    plan = _sim_plan(no_gpu, engineering, config_dir)
    code = _compose(plan, "rm", "--stop", "--force", "camera-sim", "alert-sink")
    raise typer.Exit(code)


@sim_app.command("fault")
def sim_fault(
    camera: Annotated[int, typer.Argument(help="Номер симулированной камеры (1..N)")],
    fault: Annotated[
        str,
        typer.Argument(help="none | freeze | blur | black | overexposed | occlude | shift | stop"),
    ],
) -> None:
    """Включить неисправность на симулированной камере (none — вернуть в норму)."""
    if fault not in SIM_FAULTS:
        _fail(f"неизвестная неисправность {fault!r}; допустимо: {', '.join(SIM_FAULTS)}")
    container = f"qoima-camera-sim-{camera}"
    result = run_command(
        ["docker", "exec", container, "sh", "-c", f"echo {fault} > /run/sim/fault"]
    )
    if not result.ok:
        _fail(f"не удалось изменить {container}: {result.stderr.strip()}")
    console.print(f"Камера-симулятор {camera}: {fault}")


@sim_app.command("play")
def sim_play(
    camera: Annotated[int, typer.Argument(help="Номер симулированной камеры (1..N)")],
    clip: Annotated[
        str, typer.Argument(help="Ролик относительно samples/ (e2e/fall_staged.mp4) или testsrc")
    ],
) -> None:
    """Показывать на симулированной камере ролик из samples/ по кругу (testsrc — таблица)."""
    if clip != "testsrc":
        rel = Path(clip)
        # Путь уходит в `sh -c` внутри контейнера — только безопасные символы.
        if rel.is_absolute() or ".." in rel.parts or not re.fullmatch(r"[A-Za-z0-9._/-]+", clip):
            _fail("путь ролика — относительно samples/, латиница, цифры и ._/- без «..»")
        if not (_home() / "samples" / rel).is_file():
            _fail(
                f"нет файла samples/{rel} — тестовые клипы предоставляет команда (samples/README.md)"
            )
        target = f"/samples/{rel.as_posix()}"
    else:
        target = "testsrc"
    container = f"qoima-camera-sim-{camera}"
    result = run_command(
        ["docker", "exec", container, "sh", "-c", f"printf %s '{target}' > /run/sim/source"]
    )
    if not result.ok:
        _fail(f"не удалось изменить {container}: {result.stderr.strip()}")
    console.print(f"Камера-симулятор {camera}: {target}")


@user_app.command("reset-password")
def user_reset_password(
    username: Annotated[str, typer.Argument(help="Логин пользователя, например admin")],
    no_gpu: NoGpuOpt = False,
    engineering: EngineeringOpt = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Выдать временный пароль (при входе потребуется смена). Нужна запущенная система."""
    home = _home()
    plan = _plan(_load(config_dir, home), home, run_command, no_gpu=no_gpu, engineering=engineering)
    result = run_command(
        plan.command(
            "exec", "-T", "core-api", "python", "-m", "core_api.manage", "reset-password", username
        ),
        env=plan.process_env(),
    )
    if not result.ok:
        _fail(result.stderr.strip() or "не удалось сбросить пароль (система запущена?)")
    console.print(f"Временный пароль для {username}: [bold]{result.stdout.strip()}[/]")


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
