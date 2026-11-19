"""Opt-in lifespan-owned manifest SSH forwards."""

import logging
import os
import re
from contextlib import asynccontextmanager

from dotenv import dotenv_values

from .tunnel_manager import manager

logger = logging.getLogger(__name__)


def _config(env, warehouse):
    prefix = f"MANIFEST_{warehouse}_"
    enabled = (env.get(prefix + "TUNNEL_ENABLED") or "false").lower()
    if enabled == "false":
        return None
    if enabled != "true":
        raise ValueError("TUNNEL_ENABLED must be true or false")

    def required(key):
        value = env.get(prefix + key)
        if not value:
            raise ValueError(f"missing {key}")
        return value

    def port(key):
        value = required(key)
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
            raise ValueError(f"invalid {key}")
        return int(value)

    def host(key):
        value = required(key)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value):
            raise ValueError(f"invalid {key}")
        return value

    local_host = env.get(prefix + "DB_READONLY_HOST") or env.get(prefix + "DB_HOST")
    if local_host not in ("127.0.0.1", "localhost"):
        raise ValueError("manifest DB host must be loopback")
    user = required("SSH_USER")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        raise ValueError("invalid SSH_USER")
    password = env.get(prefix + "SSH_PASSWORD") or None
    key = env.get(prefix + "SSH_KEY") or None
    if key and (not os.path.isabs(key) or any(ord(c) < 32 or ord(c) == 127 for c in key) or not os.path.isfile(key)):
        raise ValueError("invalid SSH_KEY")
    if not password and not key:
        raise ValueError("missing SSH_PASSWORD or SSH_KEY")
    return {
        "host": host("SSH_HOST"), "port": port("SSH_PORT"), "user": user,
        "password": password, "key": key, "local_port": port("DB_PORT"),
        "remote_host": host("TUNNEL_REMOTE_HOST"), "remote_port": port("TUNNEL_REMOTE_PORT"),
        "known_hosts": env.get(prefix + "KNOWN_HOSTS") or None,
    }


@asynccontextmanager
async def manifest_tunnels():
    try:
        env = {**dotenv_values(".env"), **os.environ}
    except Exception:
        logger.warning("Manifest tunnels unavailable: could not read dotenv configuration")
        yield
        return
    try:
        for warehouse in ("BGD", "CAS"):
            name = warehouse.lower()
            try:
                config = _config(env, warehouse)
            except ValueError as exc:
                logger.warning("Manifest %s tunnel unavailable: %s", warehouse, exc)
                config = None
            manager.configure(name, config)
            if config:
                manager.start(name)
        yield
    finally:
        for name in ("bgd", "cas"):
            manager.configure(name, None)
