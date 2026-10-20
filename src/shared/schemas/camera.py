"""Камеры: конфигурация, результат зондирования потока, базовая линия качества, Camera Health."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from shared.schemas.common import Contract, DisplayName, Identifier, ScenarioKind, UnitFloat
from shared.schemas.event import EventType, SyntheticKind
from shared.schemas.zone import FloorCalibration, Zone


def _validate_rtsp_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"rtsp", "rtsps"}:
        raise ValueError("ожидается URL со схемой rtsp:// или rtsps://")
    if not parts.hostname:
        raise ValueError("в URL нет адреса камеры")
    # SPEC.md §10: пароли камер хранятся только в .secrets/, никогда в конфиге и БД.
    if parts.password is not None:
        raise ValueError(
            "пароль в URL запрещён — укажите имя секрета в credentials_secret, "
            "пароль хранится в .secrets/"
        )
    return value


RtspUrl = Annotated[str, AfterValidator(_validate_rtsp_url)]


class StreamRole(StrEnum):
    MAIN = "main"
    SUB = "sub"


class VideoCodec(StrEnum):
    H264 = "h264"
    H265 = "h265"
    MJPEG = "mjpeg"
    OTHER = "other"


class CameraSource(StrEnum):
    ONVIF = "onvif"
    MANUAL = "manual"
    IMPORT = "import"
    SIMULATOR = "simulator"


class StreamProbe(Contract):
    """Результат ffprobe одного потока (SPEC.md §3.2, шаг 3)."""

    role: StreamRole
    url: RtspUrl
    codec: VideoCodec
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    fps: float = Field(gt=0, le=240)
    bitrate_kbps: int | None = Field(default=None, ge=0)
    gop_seconds: float | None = Field(default=None, gt=0)
    probed_at: AwareDatetime


class QualityBaseline(Contract):
    """Базовая линия качества кадра, снятая при подключении/калибровке камеры."""

    sharpness: float = Field(ge=0, description="Дисперсия Лапласиана на substream")
    brightness: float = Field(ge=0, le=255, description="Средняя яркость (luma)")
    noise: float = Field(ge=0, description="Оценка шума (σ)")
    snapshot_key: str = Field(min_length=1, description="Ключ снапшота в BlobStore")
    captured_at: AwareDatetime


class CameraConfig(Contract):
    id: Identifier
    name: DisplayName
    source: CameraSource
    main_url: RtspUrl
    sub_url: RtspUrl | None = None
    credentials_secret: Identifier | None = Field(
        default=None, description="Имя файла в .secrets/ с логином и паролем камеры"
    )
    enabled: bool = True
    scenarios: list[ScenarioKind] = Field(default_factory=list)
    location: str | None = Field(default=None, max_length=200)
    vendor: str | None = Field(default=None, max_length=60, description="Производитель (из ONVIF)")
    model: str | None = Field(default=None, max_length=120)
    onvif_xaddr: str | None = Field(
        default=None, max_length=500, description="Адрес ONVIF device service"
    )

    @model_validator(mode="after")
    def _unique_scenarios(self) -> Self:
        if len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("сценарии камеры повторяются")
        return self


class CameraHealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class CameraIssue(StrEnum):
    """Причины деградации камеры (SPEC.md §5.9)."""

    STREAM_DOWN = "stream_down"
    LOW_FPS = "low_fps"
    RECONNECTING = "reconnecting"
    DECODE_ERRORS = "decode_errors"
    FROZEN = "frozen"
    BLACK = "black"
    OVEREXPOSED = "overexposed"
    BLUR = "blur"
    OCCLUDED = "occluded"
    VIEW_SHIFT = "view_shift"


class CameraHealth(Contract):
    """Снимок здоровья камеры. Публикуется каждые camera_health.check_interval_s секунд."""

    camera_id: Identifier
    checked_at: AwareDatetime
    status: CameraHealthStatus
    stream_alive: bool
    fps_in: float = Field(ge=0)
    fps_planned: float = Field(ge=0)
    fps_processed: float = Field(ge=0)
    reconnects_total: int = Field(ge=0)
    decode_errors_total: int = Field(ge=0)
    last_packet_at: AwareDatetime | None = Field(
        default=None,
        description="Последний полученный пакет — по нему меряется SLO обнаружения офлайна",
    )
    brightness: float | None = Field(
        default=None, ge=0, le=255, description="Средняя яркость кадра"
    )
    frozen: bool
    black: bool
    overexposed: bool
    sharpness_ratio: float | None = Field(
        default=None, ge=0, description="Резкость относительно базовой линии; None — нет базовой"
    )
    occluded_ratio: UnitFloat | None = None
    view_shift_score: UnitFloat | None = Field(
        default=None, description="0 — ракурс совпадает со снапшотом калибровки, 1 — полный сдвиг"
    )
    issues: list[CameraIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_issues(self) -> Self:
        # «Тишина ≠ норма» (SPEC.md §2.2): статус не может противоречить найденным проблемам.
        if not self.stream_alive:
            if self.status is not CameraHealthStatus.DOWN:
                raise ValueError("поток не жив — статус должен быть down")
            if CameraIssue.STREAM_DOWN not in self.issues:
                raise ValueError("поток не жив — в issues должен быть stream_down")
        if self.issues and self.status is CameraHealthStatus.OK:
            raise ValueError("есть проблемы (issues) — статус не может быть ok")
        flags = {
            CameraIssue.FROZEN: self.frozen,
            CameraIssue.BLACK: self.black,
            CameraIssue.OVEREXPOSED: self.overexposed,
        }
        for issue, flag in flags.items():
            if flag and issue not in self.issues:
                raise ValueError(f"флаг {issue.value} выставлен, но не указан в issues")
        return self


class PipelineCommand(Contract):
    """Команда core-api → vision-pipeline (Valkey Streams). Сейчас одна: тестовое событие для
    самотеста и канарейки — настоящий клип из GOP-буфера, настоящий путь до каналов.

    event_id выбирает core-api: так он заранее знает, какое событие ждать, а повтор команды
    не создаёт второе событие.
    """

    event_id: UUID
    kind: Literal["inject_event"]
    camera_id: Identifier
    event_type: EventType
    synthetic: SyntheticKind
    zone_id: Identifier | None = None
    issued_at: AwareDatetime


class CameraRegistryEntry(Contract):
    """Камера в реестре, который core-api публикует для vision-pipeline (без учётных данных).

    Пайплайн читает потоки только через MediaMTX (SPEC.md §10): main_path и sub_path — имена путей
    в MediaMTX, а не адреса камер.
    """

    id: Identifier
    name: DisplayName
    enabled: bool
    main_path: str = Field(min_length=1, max_length=200)
    sub_path: str | None = Field(default=None, max_length=200)
    expected_fps: float | None = Field(
        default=None, gt=0, description="FPS потока анализа по зондированию"
    )
    baseline: QualityBaseline | None = None
    scenarios: list[ScenarioKind] = Field(default_factory=list)
    zones: list[Zone] = Field(default_factory=list)
    floor: FloorCalibration | None = None
