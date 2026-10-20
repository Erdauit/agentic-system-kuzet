"""Задачи автопоиска: выполняются в фоне, прогресс уходит в UI по WebSocket.

Учётные данные, подошедшие к устройствам, хранятся только в памяти задачи (до подключения
камер); на диск они попадают лишь для подключённых камер (CredentialStore).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core_api.cameras.discovery import (
    DiscoveryOutcome,
    _local_ipv4_interfaces,
    discover,
    plan_scan,
)
from core_api.cameras.onvif import Credentials
from core_api.realtime import Broadcaster
from shared.config import QoimaConfig
from shared.schemas import DiscoveryJob
from shared.telemetry import get_logger

log = get_logger("discovery-jobs")

MAX_JOBS = 10


@dataclass
class JobState:
    job: DiscoveryJob
    credentials: dict[str, Credentials] = field(default_factory=dict)
    outcome: DiscoveryOutcome | None = None


def _resolve(hostnames: list[str]) -> list[ipaddress.IPv4Address]:
    """Все адреса служебных сервисов (MediaMTX живёт в двух сетях) — их не сканируем."""
    out: list[ipaddress.IPv4Address] = []
    for name in hostnames:
        try:
            infos = socket.getaddrinfo(name, None, socket.AF_INET)
        except OSError:
            continue
        out.extend(ipaddress.IPv4Address(str(info[4][0])) for info in infos)
    return list(dict.fromkeys(out))


class DiscoveryJobs:
    def __init__(
        self,
        config: Callable[[], QoimaConfig],
        broadcaster: Broadcaster,
        host_subnets: list[str],
        exclude_hostnames: list[str],
    ) -> None:
        self.config = config
        self.broadcaster = broadcaster
        self.host_subnets = host_subnets
        self.exclude_hostnames = exclude_hostnames
        self.jobs: OrderedDict[uuid.UUID, JobState] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()

    def get(self, job_id: uuid.UUID) -> JobState | None:
        return self.jobs.get(job_id)

    def start(self, extra_subnets: list[str], credentials: list[Credentials]) -> DiscoveryJob:
        cfg = self.config().defaults.discovery
        interfaces = _local_ipv4_interfaces() if Path("/sys/class/net").is_dir() else []
        plan = plan_scan(
            [*cfg.subnets, *extra_subnets],
            self.host_subnets,
            interfaces,
            _resolve(self.exclude_hostnames),
            cfg.max_hosts,
        )
        job = DiscoveryJob(
            id=uuid.uuid4(),
            status="running",
            started_at=datetime.now(UTC),
            subnets=plan.subnets,
            hosts_total=len(plan.hosts),
            warnings=plan.warnings,
        )
        state = JobState(job=job)
        self.jobs[job.id] = state
        while len(self.jobs) > MAX_JOBS:
            self.jobs.popitem(last=False)

        async def progress(done: int, total: int) -> None:
            state.job = state.job.model_copy(update={"hosts_scanned": done})
            self.broadcaster.publish(
                {
                    "type": "discovery_progress",
                    "job_id": str(job.id),
                    "hosts_scanned": done,
                    "hosts_total": total,
                }
            )

        async def run() -> None:
            try:
                outcome = await discover(
                    plan, cfg, self.config().camera_url_templates, credentials, progress
                )
                state.outcome = outcome
                state.credentials = outcome.credentials
                state.job = state.job.model_copy(
                    update={
                        "status": "done",
                        "finished_at": datetime.now(UTC),
                        "hosts_scanned": len(plan.hosts),
                        "devices": outcome.devices,
                    }
                )
            except Exception as exc:
                log.exception("discovery_failed")
                state.job = state.job.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": datetime.now(UTC),
                        "error": str(exc)[:500],
                    }
                )
            self.broadcaster.publish(
                {"type": "discovery_done", "job": state.job.model_dump(mode="json")}
            )

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job
