"""Каналы доставки (SPEC.md §7): веб-UI, Telegram, локальное реле/сирена (HTTP или Modbus/TCP).

Канал отправляет одно сообщение одному получателю (target) и возвращает результат; повторы,
идемпотентность и эскалацию делает очередь доставок (alerts/delivery.py). Получатели:
- web_ui — "web" (операторы) или "system" (инженеры): сообщение уходит в WebSocket;
- telegram — chat id групп получателей; к алерту прикладывается кадр, позже — клип;
- local_relay — "http" и/или "modbus" (сирена: катушка включается на pulse_seconds или до
  подтверждения инцидента);
- opcua — не реализован (docs/TODO.md): доставка честно завершается ошибкой.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from shared.config.models import AlertsConfig, RelayModbus
from shared.schemas import ChannelKind
from shared.storage.secrets import SecretNotFoundError, read_secret
from shared.telemetry import get_logger

log = get_logger("alert-channels")

# Ограничения Bot API: подпись к фото/видео — до 1024 символов, файл через sendVideo — до 50 МБ.
TELEGRAM_CAPTION_MAX = 1024
TELEGRAM_VIDEO_MAX_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class AlertMessage:
    title: str
    text: str
    severity: str
    photo: bytes | None = None  # кадр-доказательство (JPEG)
    video: bytes | None = None  # клип события (MP4) — отдельным сообщением, когда готов
    payload: dict[str, object] = field(default_factory=dict)  # для веб-UI и HTTP-реле


@dataclass(frozen=True)
class DeliveryResult:
    channel: ChannelKind
    target: str
    ok: bool
    error: str | None = None


def _format(msg: AlertMessage) -> str:
    return f"[Qoima Vision] {msg.title}\n{msg.text}"


def _telegram_error(resp: httpx.Response) -> str | None:
    body = (
        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    )
    if resp.status_code == 200 and bool(body.get("ok")):
        return None
    return str(body.get("description") or f"HTTP {resp.status_code}")


async def send_telegram_one(
    http: httpx.AsyncClient,
    token: str,
    chat_id: str,
    msg: AlertMessage,
    timeout_s: float,
    api_base: str,
) -> DeliveryResult:
    base = f"{api_base.rstrip('/')}/bot{token}"
    text = _format(msg)
    try:
        if msg.video is not None and len(msg.video) <= TELEGRAM_VIDEO_MAX_BYTES:
            resp = await http.post(
                f"{base}/sendVideo",
                data={"chat_id": chat_id, "caption": text[:TELEGRAM_CAPTION_MAX]},
                files={"video": ("clip.mp4", msg.video, "video/mp4")},
                timeout=timeout_s * 6,  # загрузка клипа дольше текста
            )
        elif msg.photo is not None:
            resp = await http.post(
                f"{base}/sendPhoto",
                data={"chat_id": chat_id, "caption": text[:TELEGRAM_CAPTION_MAX]},
                files={"photo": ("frame.jpg", msg.photo, "image/jpeg")},
                timeout=timeout_s,
            )
        else:
            resp = await http.post(
                f"{base}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=timeout_s
            )
        error = _telegram_error(resp)
    except httpx.HTTPError as exc:
        error = f"Telegram недоступен: {type(exc).__name__}"
    return DeliveryResult(ChannelKind.TELEGRAM, chat_id, error is None, error)


async def send_telegram(
    http: httpx.AsyncClient,
    token: str,
    chat_ids: list[str],
    msg: AlertMessage,
    timeout_s: float,
    api_base: str = "https://api.telegram.org",
) -> list[DeliveryResult]:
    return [
        await send_telegram_one(http, token, chat_id, msg, timeout_s, api_base)
        for chat_id in chat_ids
    ]


async def send_http_relay(
    http: httpx.AsyncClient, url: str, method: str, msg: AlertMessage, timeout_s: float
) -> DeliveryResult:
    try:
        resp = await http.request(
            method,
            url,
            json={
                "title": msg.title,
                "text": msg.text,
                "severity": msg.severity,
                "source": "qoima-vision",
                **msg.payload,
            },
            timeout=timeout_s,
        )
        ok = 200 <= resp.status_code < 300
        return DeliveryResult(
            ChannelKind.LOCAL_RELAY, "http", ok, None if ok else f"HTTP {resp.status_code}"
        )
    except httpx.HTTPError as exc:
        return DeliveryResult(
            ChannelKind.LOCAL_RELAY, "http", False, f"реле недоступно: {type(exc).__name__}"
        )


async def write_modbus_coil(cfg: RelayModbus, value: bool, timeout_s: float) -> str | None:
    """Пишет катушку реле. None — успех, иначе текст ошибки."""
    from pymodbus.client import AsyncModbusTcpClient
    from pymodbus.exceptions import ModbusException

    if not cfg.host:
        return "не задан адрес Modbus-реле"
    client = AsyncModbusTcpClient(cfg.host, port=cfg.port, timeout=timeout_s, retries=1)
    try:
        if not await client.connect():
            return f"Modbus-реле {cfg.host}:{cfg.port} недоступно"
        result = await client.write_coil(cfg.coil, value, device_id=cfg.unit_id)
        if result.isError():
            return f"Modbus-реле ответило ошибкой: {result}"
        return None
    except (ModbusException, OSError) as exc:
        return f"Modbus-реле: {type(exc).__name__}: {exc}"
    finally:
        client.close()


def telegram_token(secret_name: str) -> str | None:
    try:
        return read_secret(secret_name, path_env="QOIMA_TELEGRAM_TOKEN_FILE").get_secret_value()
    except SecretNotFoundError:
        return None


# Доставка в веб-UI: возвращает, сколько открытых окон получили сообщение.
WebHook = Callable[[str, AlertMessage], int]


class ChannelDispatcher:
    """Отправка в каналы. Веб-UI — через web_hook (рассылка WebSocket из core-api)."""

    def __init__(self, http: httpx.AsyncClient, web_hook: WebHook | None = None) -> None:
        self.http = http
        self.web_hook = web_hook
        self._siren_off: dict[str, asyncio.Task[None]] = {}

    def telegram_chats(self, cfg: AlertsConfig, groups: list[str]) -> list[str]:
        chats: list[str] = []
        for g in groups:
            group = cfg.groups.get(g)
            if group is not None:
                chats.extend(group.telegram_chat_ids)
        return list(dict.fromkeys(chats))

    def targets(
        self, cfg: AlertsConfig, channel: ChannelKind, groups: list[str], web_target: str = "web"
    ) -> list[str]:
        """Получатели канала для групп. Выключенный канал — пустой список."""
        if channel is ChannelKind.WEB_UI:
            return [web_target]
        if not cfg.channels.is_enabled(channel):
            return []
        if channel is ChannelKind.TELEGRAM:
            return self.telegram_chats(cfg, groups)
        if channel is ChannelKind.LOCAL_RELAY:
            relay = cfg.channels.local_relay
            return [t for t, on in (("http", relay.http.url), ("modbus", relay.modbus.host)) if on]
        return ["opcua"]

    async def send_one(
        self, cfg: AlertsConfig, channel: ChannelKind, target: str, msg: AlertMessage
    ) -> DeliveryResult:
        if channel is ChannelKind.WEB_UI:
            windows = self.web_hook(target, msg) if self.web_hook is not None else 0
            # Алерт операторам считается доставленным, только если его кто-то видит: иначе
            # повторы, а после max_attempts — системный алерт инженерам («тишина ≠ норма»).
            # Системная лента (канарейка) — справочная, её доставка не требует открытого окна.
            if windows == 0 and target != "system":
                return DeliveryResult(
                    channel, target, False, "нет открытых окон Qoima Vision — алерт никто не видит"
                )
            return DeliveryResult(channel, target, True)
        if not cfg.channels.is_enabled(channel):
            return DeliveryResult(channel, target, False, "канал выключен в настройках")
        if channel is ChannelKind.TELEGRAM:
            token = telegram_token(cfg.channels.telegram.bot_token_secret)
            if token is None:
                return DeliveryResult(channel, target, False, "не задан токен бота")
            tg = cfg.channels.telegram
            return await send_telegram_one(
                self.http, token, target.split("#", 1)[0], msg, tg.timeout_s, tg.api_base
            )
        if channel is ChannelKind.LOCAL_RELAY:
            relay = cfg.channels.local_relay
            if target == "http" and relay.http.url:
                return await send_http_relay(
                    self.http, relay.http.url, relay.http.method, msg, relay.http.timeout_s
                )
            if target == "modbus":
                error = await write_modbus_coil(relay.modbus, True, relay.http.timeout_s)
                if error is None:
                    self._schedule_siren_off(cfg, relay.modbus.pulse_seconds)
                return DeliveryResult(channel, target, error is None, error)
            return DeliveryResult(channel, target, False, f"реле {target} не настроено")
        return DeliveryResult(
            channel, target, False, "OPC UA пока не реализован (docs/TODO.md) — используйте реле"
        )

    async def send(
        self, cfg: AlertsConfig, channel: ChannelKind, groups: list[str], msg: AlertMessage
    ) -> list[DeliveryResult]:
        """Разовая отправка всем получателям групп (системные алерты, тест канала из мастера)."""
        if channel is ChannelKind.WEB_UI:
            return [DeliveryResult(channel, "web", True)]
        if not cfg.channels.is_enabled(channel):
            return [DeliveryResult(channel, "-", False, "канал выключен в настройках")]
        if channel is ChannelKind.TELEGRAM:
            if telegram_token(cfg.channels.telegram.bot_token_secret) is None:
                return [DeliveryResult(channel, "-", False, "не задан токен бота")]
            if not self.telegram_chats(cfg, groups):
                return [DeliveryResult(channel, "-", False, "у групп получателей нет chat id")]
        targets = self.targets(cfg, channel, groups)
        if not targets:
            return [DeliveryResult(channel, "-", False, "нет получателей")]
        return [await self.send_one(cfg, channel, t, msg) for t in targets]

    # ------------------------------------------------------------ сирена

    def _schedule_siren_off(self, cfg: AlertsConfig, after_s: float) -> None:
        old = self._siren_off.pop("modbus", None)
        if old is not None:
            old.cancel()

        async def later() -> None:
            await asyncio.sleep(after_s)
            await self.siren_off(cfg)

        self._siren_off["modbus"] = asyncio.create_task(later())

    async def siren_off(self, cfg: AlertsConfig) -> None:
        """Выключить сирену (оператор подтвердил инцидент или истёк pulse_seconds)."""
        task = self._siren_off.pop("modbus", None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        relay = cfg.channels.local_relay
        if relay.enabled and relay.modbus.host:
            error = await write_modbus_coil(relay.modbus, False, relay.http.timeout_s)
            if error is not None:
                log.warning("siren_off_failed", error=error)
