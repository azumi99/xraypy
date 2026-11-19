"""Opt-in application database SSH forward."""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlsplit

from dotenv import dotenv_values

from .db import resolve_dsn, tunnel_entry
from .tunnel_manager import manager


def _config(env, settings):
    enabled = (env.get("CV_DB_TUNNEL_ENABLED") or "false").lower()
    if enabled == "false":
        return None
    if enabled != "true":
        raise ValueError("CV_DB_TUNNEL_ENABLED must be true or false")
    host = env.get("CV_DB_TUNNEL_SSH_HOST") or ""
    user = env.get("CV_DB_TUNNEL_SSH_USER") or ""
    port = env.get("CV_DB_TUNNEL_SSH_PORT") or "22"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("invalid CV_DB_TUNNEL_SSH_HOST")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        raise ValueError("invalid CV_DB_TUNNEL_SSH_USER")
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("invalid CV_DB_TUNNEL_SSH_PORT")
    key = env.get("CV_DB_TUNNEL_SSH_KEY") or None
    if key and (not os.path.isabs(key) or any(ord(c) < 32 or ord(c) == 127 or c == "%" for c in key)
                or not os.path.isfile(key)):
        raise ValueError("invalid CV_DB_TUNNEL_SSH_KEY")
    try:
        dsn = urlsplit(resolve_dsn(settings))
        valid = (dsn.scheme in ("postgres", "postgresql") and dsn.hostname == "127.0.0.1"
                 and dsn.port == 15432 and not any(
                     k.lower() in ("host", "hostaddr", "port", "service")
                     for k, _ in parse_qsl(dsn.query, keep_blank_values=True)))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("database DSN must target 127.0.0.1:15432 without host or port overrides")
    return {"host": host, "user": user, "port": int(port),
            "password": env.get("CV_DB_TUNNEL_SSH_PASSWORD") or None, "key": key,
            "known_hosts": env.get("CV_DB_TUNNEL_KNOWN_HOSTS") or None,
            "local_port": 15432, "remote_host": "127.0.0.1", "remote_port": 5432}


@asynccontextmanager
async def db_tunnel(settings):
    try:
        env = {**dotenv_values(".env"), **os.environ}
    except Exception:
        raise RuntimeError("Cannot read database tunnel configuration") from None
    config = _config(env, settings)
    entry = manager.configure("db", config)
    if entry is None:
        yield
        return
    token = tunnel_entry.set(entry)
    try:
        manager.start("db")
        deadline = asyncio.get_running_loop().time() + 10
        while not manager.status("db")["alive"]:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Cannot establish database tunnel; check local port, SSH and trusted known_hosts")
            await asyncio.sleep(0.05)
        yield
    finally:
        tunnel_entry.reset(token)
        manager.configure("db", None)
