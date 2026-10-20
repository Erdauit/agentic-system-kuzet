"""Зависимости FastAPI: текущий пользователь, роли, защита от CSRF."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from core_api.auth.service import AuthService, AuthUser
from shared.schemas import Role

SESSION_COOKIE = "qoima_session"
CSRF_HEADER = "x-qoima-request"

ADMIN_ROLES = frozenset({Role.SITE_ADMIN, Role.QOIMA_ENGINEER})


def get_auth(request: Request) -> AuthService:
    auth: AuthService = request.app.state.auth
    return auth


async def current_user_allow_password_change(request: Request) -> AuthUser:
    token = request.cookies.get(SESSION_COOKIE)
    user = await get_auth(request).resolve(token) if token else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "требуется вход")
    # Защита от CSRF: изменяющие запросы обязаны нести заголовок, который браузер не отправит
    # с чужого сайта без CORS (а CORS core-api не разрешает). Плюс cookie SameSite=Strict.
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"нет заголовка {CSRF_HEADER}")
    return user


async def current_user(
    user: Annotated[AuthUser, Depends(current_user_allow_password_change)],
) -> AuthUser:
    if user.must_change_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нужно сменить временный пароль")
    return user


def require_roles(*roles: Role) -> Callable[[AuthUser], Awaitable[AuthUser]]:
    allowed = frozenset(roles)

    async def check(user: Annotated[AuthUser, Depends(current_user)]) -> AuthUser:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "недостаточно прав для этого действия")
        return user

    return check


CurrentUser = Annotated[AuthUser, Depends(current_user)]
AdminUser = Annotated[AuthUser, Depends(require_roles(*ADMIN_ROLES))]
