"""Каталоги бенчмарка должны быть доступны пользователю контейнера после установки."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli.main import BENCH_DIR_IN_CONTAINER
from cli.paths import DATA_DIRS

ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_output_is_a_managed_data_directory() -> None:
    relative = str(Path(BENCH_DIR_IN_CONTAINER).relative_to("/var/lib/qoima"))
    assert DATA_DIRS.get(relative) == (10001, 10001)


@pytest.mark.skipif(sys.platform != "linux", reason="права Linux проверяются на Linux")
def test_installer_prepares_writable_benchmark_directory(tmp_path: Path) -> None:
    script = (ROOT / "install.sh").read_text()
    directory_setup = (
        "make_dir()"
        + script.split("make_dir()", 1)[1].split(
            "# ---------------------------------------------------------------- 5.", 1
        )[0]
    )
    subprocess.run(
        ["bash", "-eu", "-c", directory_setup],
        env={**os.environ, "DATA_ROOT": str(tmp_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    output = tmp_path / "bench"
    assert output.is_dir(), "install.sh не подготовил каталог вывода бенчмарка"
    if os.geteuid() == 0:
        assert (output.stat().st_uid, output.stat().st_gid) == (10001, 10001)
        assert output.stat().st_mode & 0o200
    else:
        assert output.stat().st_mode & 0o002  # --dev: запись из контейнера под другим uid
