"""Мастер настройки: шаг 1 (объект), шаг 2 (железо — отчёт doctor), шаг 7 (алерты),
шаг 8 (сквозной самотест) и канарейка пути алерта."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from core_api.alerts.channels import AlertMessage
from core_api.api_models import ApiModel, ChannelTestIn, DeliveryOut, SiteIn, TelegramTokenIn
from core_api.audit import write_audit
from core_api.auth.deps import AdminUser, CurrentUser
from core_api.capacity.hardware import hardware_verdict
from core_api.context import AppContext, get_ctx
from core_api.doctor_report import load_report, report_age_minutes
from shared.config import ConfigError
from shared.schemas import ChannelKind, DoctorReport, StreamProbe

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])


def write_secret_file(target: Path, value: str) -> None:
    """Атомарная запись секрета с правами 600 (читает только core-api)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        Path(tmp).replace(target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def _apply(ctx: AppContext, patch: dict[str, Any], actor: str, what: str) -> None:
    try:
        ctx.config = ctx.site.update(patch)
    except ConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(exc.errors)) from exc
    async with ctx.cameras.sessions() as s:
        await write_audit(s, actor=actor, action="config_change", target=what, details=patch)
        await s.commit()
    await ctx.valkey.publish("qoima:config:changed", what)


@router.get("/status")
async def setup_status(_user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    site_saved = "site" in (ctx.site.read().get("defaults") or {})
    cameras = await ctx.cameras.list_all()
    report = load_report(ctx.data_root)
    return {
        "site_saved": site_saved,
        "cameras": len(cameras),
        "doctor_report": report is not None,
        "telegram_enabled": ctx.config.alerts.channels.telegram.enabled,
    }


# ---------------------------------------------------------------- шаг 1


@router.get("/site", response_model=SiteIn)
async def get_site(_user: CurrentUser, ctx: Ctx) -> SiteIn:
    site = ctx.config.defaults.site
    return SiteIn.model_validate(
        {
            "name": site.name,
            "ui_languages": [lang.value for lang in site.ui_languages],
            "default_language": site.default_language.value,
            "timezone": site.timezone,
            "shifts": [
                {"name": s.name, "start": s.start.strftime("%H:%M"), "end": s.end.strftime("%H:%M")}
                for s in site.shifts
            ],
            "discovery_subnets": ctx.config.defaults.discovery.subnets,
        }
    )


@router.put("/site", response_model=SiteIn)
async def put_site(body: SiteIn, user: AdminUser, ctx: Ctx) -> SiteIn:
    patch = {
        "defaults": {
            "site": {
                "name": body.name,
                "ui_languages": body.ui_languages,
                "default_language": body.default_language,
                "timezone": body.timezone,
                "shifts": [s.model_dump() for s in body.shifts],
            },
            "discovery": {"subnets": body.discovery_subnets},
        }
    }
    await _apply(ctx, patch, user.actor, "site")
    return body


# ---------------------------------------------------------------- шаг 2


@router.get("/hardware")
async def hardware(_user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    """Итог «подходит / с ограничениями / не подходит» по отчёту doctor и последнему bench."""
    report: DoctorReport | None = load_report(ctx.data_root)
    bench = await asyncio.to_thread(ctx.capacity.bench)
    probes = [
        [StreamProbe.model_validate(p) for p in row.probes or []]
        for row in await ctx.cameras.list_all()
        if row.enabled
    ]
    verdict, items = hardware_verdict(report, bench, ctx.config, probes)
    out: dict[str, Any] = {
        "verdict": verdict.value,
        "items": [{"name": i.name, "level": i.level, "detail": i.detail} for i in items],
        "report": report.model_dump(mode="json") if report else None,
        "bench_id": bench.id if bench else None,
    }
    if report is not None:
        age = report_age_minutes(report)
        out["age_minutes"] = round(age, 1)
        out["stale"] = age > ctx.config.defaults.system_alerts.doctor_report_max_age_minutes
    else:
        out["message"] = "Отчёта нет. На сервере выполните: qoima doctor --report"
    return out


# ---------------------------------------------------------------- шаг 7


@router.get("/alerts")
async def get_alerts(_user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    cfg = ctx.config.alerts
    data = cfg.model_dump(mode="json")
    data["telegram_token_set"] = (
        ctx.runtime_secrets / cfg.channels.telegram.bot_token_secret
    ).is_file()
    return data


@router.put("/alerts")
async def put_alerts(body: dict[str, Any], user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    """Тело — раздел alerts в формате configs/alerts.yaml (каналы, группы, эскалация).
    Полная проверка — QoimaConfig: системные алерты не попадут операторам и т.д."""
    allowed = {"channels", "delivery", "groups", "safety", "system"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"неизвестные поля: {sorted(unknown)}"
        )
    await _apply(ctx, {"alerts": body}, user.actor, "alerts")
    return await get_alerts(user, ctx)


@router.put("/alerts/telegram-token", status_code=status.HTTP_204_NO_CONTENT)
async def put_telegram_token(body: TelegramTokenIn, user: AdminUser, ctx: Ctx) -> None:
    name = ctx.config.alerts.channels.telegram.bot_token_secret
    await asyncio.to_thread(
        write_secret_file, ctx.runtime_secrets / name, body.token.get_secret_value()
    )
    async with ctx.cameras.sessions() as s:
        await write_audit(s, actor=user.actor, action="secret_change", target="telegram_bot_token")
        await s.commit()


@router.post("/alerts/test", response_model=list[DeliveryOut])
async def test_channel(body: ChannelTestIn, user: AdminUser, ctx: Ctx) -> list[DeliveryOut]:
    cfg = ctx.config.alerts
    channel = ChannelKind(body.channel)
    if body.stream == "system":
        groups = cfg.system.groups
    else:
        groups = sorted({g for lvl in cfg.safety.escalation for g in lvl.groups})
    msg = AlertMessage(
        title="Тестовое сообщение",
        text=f"Проверка канала {channel.value} из мастера настройки ({user.username}). Действий не требуется.",
        severity="info",
    )
    if channel is ChannelKind.WEB_UI:
        ctx.broadcaster.publish({"type": "test_message", "text": msg.text})
    results = await ctx.dispatcher.send(cfg, channel, groups, msg)
    async with ctx.cameras.sessions() as s:
        await write_audit(
            s,
            actor=user.actor,
            action="channel_test",
            target=channel.value,
            details={"ok": all(r.ok for r in results), "targets": len(results)},
        )
        await s.commit()
    return [
        DeliveryOut(channel=r.channel.value, target=r.target, ok=r.ok, error=r.error)
        for r in results
    ]


# ---------------------------------------------------------------- шаг 8: сквозной самотест


class SelfTestIn(ApiModel):
    camera_ids: list[str] = Field(min_length=1, max_length=32)


@router.post("/selftest", status_code=status.HTTP_202_ACCEPTED)
async def start_selftest(body: SelfTestIn, user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    """Тестовое событие на камерах → алерт с кадром и клипом во все каналы (пометка «САМОТЕСТ»)."""
    try:
        run = await ctx.selftest.start(body.camera_ids, user.actor)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await ctx.selftest.status(run.id) or {}


@router.get("/selftest/{run_id}")
async def selftest_status(run_id: uuid.UUID, _user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    result = await ctx.selftest.status(run_id)
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "самотест не найден (после рестарта — запустите заново)"
        )
    return result


@router.post("/selftest/{run_id}/confirm")
async def selftest_confirm(run_id: uuid.UUID, user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    """Человек подтверждает, что алерты дошли во все каналы."""
    result = await ctx.selftest.confirm(run_id, user.actor)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "самотест не найден")
    return result


@router.get("/canary")
async def canary_status(_user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    cfg = ctx.config.defaults.canary
    return {
        "enabled": cfg.alert_path_enabled,
        "interval_minutes": cfg.interval_minutes,
        "last": ctx.selftest.last_canary,
    }


@router.post("/canary/run")
async def canary_run(user: AdminUser, ctx: Ctx) -> dict[str, Any]:
    async with ctx.cameras.sessions() as s:
        await write_audit(s, actor=user.actor, action="canary_run", target="canary")
        await s.commit()
    return await ctx.selftest.canary_once()
