"""Global administrator endpoints for warehouse accounts."""

import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .security import identity

router = APIRouter(prefix="/users", tags=["users"])


class UserAccess(BaseModel):
    access: Literal["bgd", "cas"]
    role: Literal["operator", "bc", "label_print"]


class CreateUser(UserAccess):
    email: str = Field(max_length=254)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s/@]+@[^\s/@]+\.[^\s/@]+", value):
            raise ValueError("Email tidak valid")
        return value


class UpdateUser(UserAccess):
    active: bool
    password: str | None = Field(default=None, min_length=12, max_length=128)


def admin_store(request: Request):
    if (identity(request) or {}).get("role") != "admin":
        raise HTTPException(403, "forbidden")
    return request.app.state.auth_store


@router.get("")
async def list_users(request: Request):
    return await admin_store(request).list_users()


@router.post("", status_code=201)
async def create_user(body: CreateUser, request: Request):
    if not await admin_store(request).create_user(body.email, body.access, body.role, body.password):
        raise HTTPException(409, "Email sudah terdaftar")
    return {"email": body.email, "access": body.access, "role": body.role, "active": True}


@router.put("/{email}")
async def update_user(email: str, body: UpdateUser, request: Request):
    if not await admin_store(request).update_user(email, body.access, body.role, body.active, body.password):
        raise HTTPException(404, "Akun yang bisa dikelola tidak ditemukan")
    return {"email": email, "access": body.access, "role": body.role, "active": body.active}
