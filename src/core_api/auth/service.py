"""Вход, сессии и пользователи."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from core_api.audit import write_audit
from core_api.auth.passwords import hash_password, verify_password
from core_api.db import models
from shared.config.models import AuthConfig
from shared.schemas import Role
from shared.telemetry import get_logger

log = get_logger("auth")


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    username: str
    display_name: str
    role: Role
    must_change_password: bool

    @property
    def actor(self) -> str:
        return f"{self.role.value}:{self.username}"


class AuthError(Exception):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _user(row: models.User) -> AuthUser:
    return AuthUser(
        row.id, row.username, row.display_name, Role(row.role), row.must_change_password
    )


class AuthService:
    def __init__(self, engine: AsyncEngine, cfg: AuthConfig) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.cfg = cfg

    def check_password_policy(self, password: str) -> None:
        if len(password) < self.cfg.password_min_length:
            raise AuthError(f"пароль короче {self.cfg.password_min_length} символов")

    async def login(
        self, username: str, password: str, ip: str | None, user_agent: str | None
    ) -> tuple[str, AuthUser]:
        now = datetime.now(UTC)
        async with self.sessions() as s:
            row = await s.scalar(select(models.User).where(models.User.username == username))
            if row is None or not row.is_active:
                await asyncio.to_thread(verify_password, password, None)
                await write_audit(
                    s, actor=f"anonymous:{username[:60]}", action="login_failed", details={"ip": ip}
                )
                await s.commit()
                raise AuthError("неверный логин или пароль")
            if row.locked_until is not None and row.locked_until > now:
                raise AuthError(
                    "вход временно заблокирован после серии неудачных попыток — подождите несколько минут"
                )
            ok = await asyncio.to_thread(verify_password, password, row.password_hash)
            if not ok:
                row.failed_logins += 1
                if row.failed_logins >= self.cfg.max_failed_logins:
                    row.locked_until = now + timedelta(minutes=self.cfg.lockout_minutes)
                    row.failed_logins = 0
                await write_audit(
                    s, actor=f"{row.role}:{row.username}", action="login_failed", details={"ip": ip}
                )
                await s.commit()
                raise AuthError("неверный логин или пароль")
            row.failed_logins = 0
            row.locked_until = None
            row.last_login_at = now
            token = secrets.token_urlsafe(32)
            s.add(
                models.Session(
                    token_hash=token_hash(token),
                    user_id=row.id,
                    expires_at=now + timedelta(hours=self.cfg.session_ttl_hours),
                    ip=(ip or "")[:64] or None,
                    user_agent=(user_agent or "")[:300] or None,
                )
            )
            await write_audit(
                s, actor=f"{row.role}:{row.username}", action="login", details={"ip": ip}
            )
            await s.commit()
            return token, _user(row)

    async def resolve(self, token: str) -> AuthUser | None:
        now = datetime.now(UTC)
        async with self.sessions() as s:
            result = await s.execute(
                select(models.Session, models.User)
                .join(models.User, models.User.id == models.Session.user_id)
                .where(
                    models.Session.token_hash == token_hash(token), models.Session.expires_at > now
                )
            )
            pair = result.first()
            if pair is None or not pair[1].is_active:
                return None
            session_row, user_row = pair
            if (now - session_row.last_seen_at).total_seconds() > 60:
                session_row.last_seen_at = now
                await s.commit()
            return _user(user_row)

    async def logout(self, token: str, user: AuthUser) -> None:
        async with self.sessions() as s:
            await s.execute(
                delete(models.Session).where(models.Session.token_hash == token_hash(token))
            )
            await write_audit(s, actor=user.actor, action="logout")
            await s.commit()

    async def change_password(self, user: AuthUser, current: str, new: str) -> None:
        self.check_password_policy(new)
        if current == new:
            raise AuthError("новый пароль совпадает с текущим")
        async with self.sessions() as s:
            row = await s.get(models.User, user.id)
            if row is None or not await asyncio.to_thread(
                verify_password, current, row.password_hash
            ):
                raise AuthError("текущий пароль указан неверно")
            row.password_hash = await asyncio.to_thread(hash_password, new)
            row.must_change_password = False
            await write_audit(
                s, actor=user.actor, action="password_change", target=f"user:{row.username}"
            )
            await s.commit()

    async def set_temporary_password(self, username: str, password: str, actor: str) -> None:
        async with self.sessions() as s:
            row = await s.scalar(select(models.User).where(models.User.username == username))
            if row is None:
                raise AuthError(f"пользователь {username} не найден")
            row.password_hash = await asyncio.to_thread(hash_password, password)
            row.must_change_password = True
            row.failed_logins = 0
            row.locked_until = None
            await s.execute(delete(models.Session).where(models.Session.user_id == row.id))
            await write_audit(s, actor=actor, action="password_reset", target=f"user:{username}")
            await s.commit()

    async def create_user(
        self, username: str, display_name: str, role: Role, temp_password: str, actor: str
    ) -> AuthUser:
        async with self.sessions() as s:
            if (
                await s.scalar(select(models.User).where(models.User.username == username))
                is not None
            ):
                raise AuthError(f"пользователь {username} уже существует")
            row = models.User(
                id=uuid.uuid4(),
                username=username,
                display_name=display_name,
                role=role.value,
                password_hash=await asyncio.to_thread(hash_password, temp_password),
                must_change_password=True,
            )
            s.add(row)
            await write_audit(
                s,
                actor=actor,
                action="user_create",
                target=f"user:{username}",
                details={"role": role.value},
            )
            await s.commit()
            return _user(row)

    async def list_users(self) -> list[models.User]:
        async with self.sessions() as s:
            return list((await s.scalars(select(models.User).order_by(models.User.username))).all())

    async def update_user(
        self, user_id: uuid.UUID, changes: dict[str, object], actor: str
    ) -> models.User:
        async with self.sessions() as s:
            row = await s.get(models.User, user_id)
            if row is None:
                raise AuthError("пользователь не найден")
            for key in ("display_name", "role", "is_active"):
                if key in changes:
                    setattr(row, key, changes[key])
            if changes.get("is_active") is False:
                await s.execute(delete(models.Session).where(models.Session.user_id == row.id))
            await write_audit(
                s,
                actor=actor,
                action="user_change",
                target=f"user:{row.username}",
                details={k: str(v) for k, v in changes.items()},
            )
            await s.commit()
            return row

    async def bootstrap_admin(self, password: str | None) -> bool:
        """Первый запуск: если пользователей нет, создаётся admin (site_admin) с паролем из
        секрета admin_initial_password и обязательной сменой пароля при первом входе."""
        async with self.sessions() as s:
            count = (await s.execute(select(func.count()).select_from(models.User))).scalar_one()
            if count:
                return False
            if not password:
                log.warning("bootstrap_admin_skipped", reason="нет секрета admin_initial_password")
                return False
            s.add(
                models.User(
                    id=uuid.uuid4(),
                    username="admin",
                    display_name="Администратор",
                    role=Role.SITE_ADMIN.value,
                    password_hash=await asyncio.to_thread(hash_password, password),
                    must_change_password=True,
                )
            )
            await write_audit(
                s,
                actor="system",
                action="user_create",
                target="user:admin",
                details={"role": "site_admin"},
            )
            await s.commit()
            log.info("bootstrap_admin_created")
            return True

    async def purge_expired(self) -> None:
        async with self.sessions() as s:
            await s.execute(
                delete(models.Session).where(models.Session.expires_at < datetime.now(UTC))
            )
            await s.execute(
                update(models.User)
                .where(models.User.locked_until < datetime.now(UTC))
                .values(locked_until=None)
            )
            await s.commit()
