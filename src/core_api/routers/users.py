"""Пользователи (site_admin, qoima_engineer). Роль qoima_engineer назначает только qoima_engineer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from core_api.api_models import TemporaryPasswordOut, UserCreateIn, UserOut, UserPatchIn
from core_api.auth.deps import AdminUser, get_auth
from core_api.auth.passwords import generate_password
from core_api.auth.service import AuthError, AuthService
from core_api.db import models
from shared.schemas import Role

router = APIRouter(prefix="/api/v1/users", tags=["users"])
Auth = Annotated[AuthService, Depends(get_auth)]


def _out(row: models.User) -> UserOut:
    return UserOut(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        role=Role(row.role),
        is_active=row.is_active,
        must_change_password=row.must_change_password,
    )


def _check_role_grant(actor_role: Role, role: Role) -> None:
    if role is Role.QOIMA_ENGINEER and actor_role is not Role.QOIMA_ENGINEER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "роль qoima_engineer назначает только инженер Qoima"
        )


@router.get("", response_model=list[UserOut])
async def list_users(_user: AdminUser, auth: Auth) -> list[UserOut]:
    return [_out(r) for r in await auth.list_users()]


@router.post("", response_model=TemporaryPasswordOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreateIn, user: AdminUser, auth: Auth) -> TemporaryPasswordOut:
    _check_role_grant(user.role, body.role)
    password = generate_password()
    try:
        await auth.create_user(body.username, body.display_name, body.role, password, user.actor)
    except AuthError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TemporaryPasswordOut(username=body.username, temporary_password=password)


@router.patch("/{user_id}", response_model=UserOut)
async def patch_user(user_id: uuid.UUID, body: UserPatchIn, user: AdminUser, auth: Auth) -> UserOut:
    changes = body.model_dump(exclude_none=True)
    if "role" in changes:
        _check_role_grant(user.role, body.role or Role.OPERATOR)
        changes["role"] = changes["role"].value
    if user_id == user.id and changes.get("is_active") is False:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "нельзя отключить самого себя")
    try:
        return _out(await auth.update_user(user_id, changes, user.actor))
    except AuthError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{user_id}/reset-password", response_model=TemporaryPasswordOut)
async def reset_password(user_id: uuid.UUID, user: AdminUser, auth: Auth) -> TemporaryPasswordOut:
    target = next((r for r in await auth.list_users() if r.id == user_id), None)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "пользователь не найден")
    _check_role_grant(user.role, Role(target.role))
    password = generate_password()
    await auth.set_temporary_password(target.username, password, user.actor)
    return TemporaryPasswordOut(username=target.username, temporary_password=password)
