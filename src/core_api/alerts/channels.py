"""Каналы доставки (Фаза 1): веб-UI, Telegram, HTTP-реле.

Фаза 1 использует каналы для системных алертов и тестовой отправки из мастера (шаг 7).
Маршрутизация инцидентов, эскалация, сирена по Modbus и очередь at-least-once — Фаза 2.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from shared.config.models import AlertsConfig
from shared.schemas import ChannelKind
from shared.storage.secrets import SecretNotFoundError, read_secret


@dataclass(frozen=True)
class AlertMessage:
    title: str
    text: str
    severity: str


@dataclass(frozen=True)
class DeliveryResult:
    channel: ChannelKind
    target: str
    ok: bool
    error: str | None = None


def _format(msg: AlertMessage) -> str:
    return f"[Qoima Vision] {msg.title}\n{msg.text}"


async def send_telegram(
    http: httpx.AsyncClient, token: str, chat_ids: list[str], msg: AlertMessage, timeout_s: float
) -> list[DeliveryResult]:
    results = []
    for chat_id in chat_ids:
        try:
            resp = await http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": _format(msg)},
                timeout=timeout_s,
            )
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            ok = resp.status_code == 200 and bool(body.get("ok"))
            error = None if ok else str(body.get("description") or f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            ok, error = False, f"Telegram недоступен: {type(exc).__name__}"
        results.append(DeliveryResult(ChannelKind.TELEGRAM, chat_id, ok, error))
    return results


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
            },
            timeout=timeout_s,
        )
        ok = 200 <= resp.status_code < 300
        return DeliveryResult(
            ChannelKind.LOCAL_RELAY, url, ok, None if ok else f"HTTP {resp.status_code}"
        )
    except httpx.HTTPError as exc:
        return DeliveryResult(
            ChannelKind.LOCAL_RELAY, url, False, f"реле недоступно: {type(exc).__name__}"
        )


def telegram_token(secret_name: str) -> str | None:
    try:
        return read_secret(secret_name, path_env="QOIMA_TELEGRAM_TOKEN_FILE").get_secret_value()
    except SecretNotFoundError:
        return None


class ChannelDispatcher:
    """Отправка сообщения в каналы. Веб-UI доставляется через WebSocket вызывающим кодом."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    def telegram_chats(self, cfg: AlertsConfig, groups: list[str]) -> list[str]:
        chats: list[str] = []
        for g in groups:
            group = cfg.groups.get(g)
            if group is not None:
                chats.extend(group.telegram_chat_ids)
        return list(dict.fromkeys(chats))

    async def send(
        self, cfg: AlertsConfig, channel: ChannelKind, groups: list[str], msg: AlertMessage
    ) -> list[DeliveryResult]:
        if channel is ChannelKind.WEB_UI:
            return [DeliveryResult(channel, "web", True)]
        if not cfg.channels.is_enabled(channel):
            return [DeliveryResult(channel, "-", False, "канал выключен в настройках")]
        if channel is ChannelKind.TELEGRAM:
            token = telegram_token(cfg.channels.telegram.bot_token_secret)
            if token is None:
                return [DeliveryResult(channel, "-", False, "не задан токен бота")]
            chats = self.telegram_chats(cfg, groups)
            if not chats:
                return [DeliveryResult(channel, "-", False, "у групп получателей нет chat id")]
            return await send_telegram(
                self.http, token, chats, msg, cfg.channels.telegram.timeout_s
            )
        if channel is ChannelKind.LOCAL_RELAY:
            relay = cfg.channels.local_relay
            if relay.http.url:
                return [
                    await send_http_relay(
                        self.http, relay.http.url, relay.http.method, msg, relay.http.timeout_s
                    )
                ]
            return [DeliveryResult(channel, "-", False, "Modbus-реле подключается в Фазе 2")]
        return [DeliveryResult(channel, "-", False, "OPC UA подключается в Фазе 2")]
