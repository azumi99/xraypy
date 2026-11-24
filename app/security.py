"""Signed browser sessions shared by HTTP and WebSocket endpoints."""

import secrets
import time
from urllib.parse import urlsplit

import jwt
from fastapi import HTTPException, Request, WebSocket, status

COOKIE = "cv_session"
SESSION_SECONDS = 90 * 24 * 60 * 60  # kedaluwarsa maksimal: 3 bulan
REFRESH_AFTER = SESSION_SECONDS // 2  # digulirkan bila tersisa < 45 hari


def same_origin(connection) -> bool:
    origin = connection.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc == connection.headers.get("host")


def issue_session(settings, user: dict) -> tuple[str, dict]:
    now = int(time.time())
    claims = {
        "sub": user["email"], "access": user["access"], "iat": now,
        "exp": now + SESSION_SECONDS, "jti": secrets.token_urlsafe(32),
    }
    if user.get("role") == "bc":
        claims["role"] = "bc"
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256"), claims


def session_claims(connection) -> dict | None:
    token = connection.cookies.get(COOKIE)
    secret = getattr(connection.app.state.settings, "jwt_secret", None)
    if not token or not secret:
        return None
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "iat", "sub", "jti", "access"]})
        if claims["access"] not in ("cas", "bgd") or not isinstance(claims["sub"], str):
            return None
        # ponytail: denylist logout di memori; restart mengosongkannya, tetapi
        # logout normal juga menghapus cookie di klien.
        if claims["jti"] in getattr(connection.app.state, "revoked_jtis", ()):
            return None
        return claims
    except (jwt.InvalidTokenError, TypeError, KeyError):
        return None


def identity(connection):
    claims = session_claims(connection)
    if claims is None:
        return None
    return public_user({"email": claims["sub"], "access": claims["access"], "role": claims.get("role")})


def public_user(user: dict) -> dict:
    result = {"email": user["email"], "access": user["access"]}
    if user.get("role") == "bc":
        result["role"] = "bc"
    return result


def authorized(connection) -> bool:
    return identity(connection) is not None


def require_operator(request: Request) -> None:
    if not authorized(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


async def require_operator_ws(websocket: WebSocket) -> bool:
    if same_origin(websocket) and authorized(websocket):
        return True
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="authentication required")
    return False
