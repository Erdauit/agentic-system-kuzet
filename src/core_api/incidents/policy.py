"""Правила инцидентов без БД (SPEC.md §7): ключ дедупликации, окно объединения, важность,
кому и куда уходит алерт, текст сообщения.

Дедупликация: события одного типа на одном участке (area) — один инцидент, даже с разных камер;
без участка — по камере и зоне. Активный инцидент (OPEN/ACKNOWLEDGED) поглощает новые события,
пока с последнего события прошло ≤ dedup_window_s или с открытия ≤ cooldown_s. Закрытый
инцидент не поглощает ничего: новое падение после закрытия — новый алерт (fail-open, §2.1).
Служебные события (самотест, канарейка) не смешиваются с настоящими.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.config.models import AlertsConfig, IncidentsConfig
from shared.schemas import (
    ChannelKind,
    Event,
    EventType,
    IncidentState,
    Language,
    RoutingDecision,
    Severity,
    SyntheticKind,
)

SEVERITY: dict[EventType, Severity] = {
    EventType.FALL: Severity.CRITICAL,
    EventType.SMOKE_FIRE: Severity.CRITICAL,
    EventType.RED_ZONE: Severity.HIGH,
}


def dedup_key(event: Event) -> str:
    prefix = f"{event.synthetic.value}:" if event.synthetic else ""
    if event.area:
        return f"{prefix}{event.type.value}:area:{event.area}"
    return f"{prefix}{event.type.value}:camera:{event.camera_id}:{event.zone_id or '*'}"


def joins(
    state: IncidentState,
    opened_at: datetime,
    last_event_at: datetime | None,
    event_at: datetime,
    cfg: IncidentsConfig,
) -> bool:
    if state not in (IncidentState.OPEN, IncidentState.ACKNOWLEDGED):
        return False
    last = last_event_at or opened_at
    return (event_at - last).total_seconds() <= cfg.dedup_window_s or (
        event_at - opened_at
    ).total_seconds() <= cfg.cooldown_s


@dataclass(frozen=True)
class Route:
    channel: ChannelKind
    groups: list[str]
    web_target: str = "web"


def routes(cfg: AlertsConfig, level: int, synthetic: SyntheticKind | None) -> list[Route]:
    """Каналы и группы уровня эскалации.

    Канарейка — только системная лента веб-UI (инженеры), без Telegram и сирены: она идёт
    каждые 30 минут и не должна беспокоить людей. Самотест — сразу во все каналы всех уровней
    (мастер, шаг 8: «алерт во всех каналах», человек подтверждает получение).
    """
    if synthetic is SyntheticKind.CANARY:
        return [Route(ChannelKind.WEB_UI, cfg.system.groups, "system")]
    if synthetic is SyntheticKind.SELFTEST:
        out: dict[ChannelKind, list[str]] = {}
        for lvl in cfg.safety.escalation:
            for ch in lvl.channels:
                out.setdefault(ch, [])
                out[ch] = list(dict.fromkeys([*out[ch], *lvl.groups]))
        return [Route(ch, groups) for ch, groups in out.items()]
    lvl = cfg.safety.escalation[level]
    return [Route(ch, list(lvl.groups)) for ch in lvl.channels]


_TITLES: dict[Language, dict[EventType, str]] = {
    Language.RU: {
        EventType.FALL: "Падение человека",
        EventType.RED_ZONE: "Человек в опасной зоне",
        EventType.SMOKE_FIRE: "Дым или огонь",
    },
    Language.KK: {
        EventType.FALL: "Адамның құлауы",
        EventType.RED_ZONE: "Адам қауіпті аймақта",
        EventType.SMOKE_FIRE: "Түтін немесе от",
    },
}
_WORDS: dict[Language, dict[str, str]] = {
    Language.RU: {
        "camera": "Камера",
        "zone": "зона",
        "at": "в",
        "ack": "Подтвердите в Qoima Vision.",
        "selftest": "САМОТЕСТ",
        "canary": "Канарейка",
        "review": "требует проверки",
        "level": "эскалация, уровень",
        "clip": "Клип события",
    },
    Language.KK: {
        "camera": "Камера",
        "zone": "аймақ",
        "at": "уақыты",
        "ack": "Qoima Vision жүйесінде растаңыз.",
        "selftest": "ӨЗІН-ӨЗІ ТЕКСЕРУ",
        "canary": "Бақылау сынағы",
        "review": "тексеруді қажет етеді",
        "level": "эскалация, деңгей",
        "clip": "Оқиға бейнесі",
    },
}


@dataclass(frozen=True)
class AlertText:
    title: str
    text: str


def alert_text(
    lang: Language,
    event_type: EventType,
    camera_name: str,
    zone_name: str | None,
    local_time: str,
    routing: RoutingDecision,
    synthetic: SyntheticKind | None,
    level: int,
    clip: bool = False,
) -> AlertText:
    w = _WORDS[lang]
    title = _TITLES[lang][event_type]
    if synthetic is SyntheticKind.SELFTEST:
        title = f"{w['selftest']}: {title}"
    elif synthetic is SyntheticKind.CANARY:
        title = f"{w['canary']}: {title}"
    if routing is RoutingDecision.EVENT_WITH_ARBITER:
        title = f"{title} ({w['review']})"
    if clip:
        title = f"{w['clip']}: {title}"
    where = f"{w['camera']} «{camera_name}»"
    if zone_name:
        where += f", {w['zone']} «{zone_name}»"
    lines = [f"{where}, {w['at']} {local_time}."]
    if level > 0:
        lines.append(f"{w['level']} {level}.")
    if not clip:
        lines.append(w["ack"])
    return AlertText(title, " ".join(lines))
