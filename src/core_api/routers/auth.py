"""Вход, выход, текущий пользователь, смена пароля."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from core_api.api_models import ChangePasswordIn, LoginIn, UserOut
from core_api.auth.deps import SESSION_COOKIE, current_user_allow_password_change, get_auth
from core_api.auth.service import AuthError, AuthService, AuthUser

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

AnyUser = Annotated[AuthUser, Depends(current_user_allow_password_change)]
Auth = Annotated[AuthService, Depends(get_auth)]


def _out(user: AuthUser) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        must_change_password=user.must_change_password,
    )


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, request: Request, response: Response, auth: Auth) -> UserOut:
    # Вход — единственный изменяющий запрос без сессии; CSRF-заголовок проверяем и здесь.
    if request.headers.get("x-qoima-request") != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "нет заголовка x-qoima-request")
    try:
        token, user = await auth.login(
            body.username,
            body.password.get_secret_value(),
            request.client.host if request.client else None,
            request.headers.get("user-agent"),
        )
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(auth.cfg.session_ttl_hours * 3600),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return _out(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, user: AnyUser, auth: Auth) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await auth.logout(token, user)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: AnyUser) -> UserOut:
    return _out(user)


@router.post("/change-password", response_model=UserOut)
async def change_password(body: ChangePasswordIn, user: AnyUser, auth: Auth) -> UserOut:
    try:
        await auth.change_password(
            user, body.current_password.get_secret_value(), body.new_password.get_secret_value()
        )
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return _out(
        AuthUser(user.id, user.username, user.display_name, user.role, must_change_password=False)
    )
