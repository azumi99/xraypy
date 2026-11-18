"""Opt-in SSH forward owned by the application lifespan."""

import asyncio
import logging
import os
import re
import shutil
import socket
import tempfile
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlsplit

from dotenv import dotenv_values

from .db import resolve_dsn, tunnel_entry
from . import manifest_tunnels as common

logger = logging.getLogger(__name__)


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
                     key.lower() in ("host", "hostaddr", "port", "service")
                     for key, _ in parse_qsl(dsn.query, keep_blank_values=True)))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("database DSN must target 127.0.0.1:15432 without host or port overrides")
    return {"host": host, "user": user, "port": int(port),
            "password": env.get("CV_DB_TUNNEL_SSH_PASSWORD") or None, "key": key, "local_port": 15432}


async def _spawn(config, control_path):
    password = config["password"]
    env = os.environ.copy()
    env.pop("SSHPASS", None)
    if password:
        env["SSHPASS"] = password
    command = (["sshpass", "-e"] if password else []) + [
        "ssh", "-F", "/dev/null", "-N", "-T", "-M", "-S", control_path,
        "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
        "-o", "NumberOfPasswordPrompts=1" if password and not config["key"] else "BatchMode=yes",
    ] + (["-i", config["key"], "-o", "IdentitiesOnly=yes"] if config["key"] else []) + [
        "-L", "127.0.0.1:15432:127.0.0.1:5432",
        "-p", str(config["port"]), "-l", config["user"], config["host"],
    ]
    return await asyncio.create_subprocess_exec(
        *command, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


def _check_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 15432))


async def _start(entry):
    _check_port()
    # A dead SSH master can leave its socket behind; never accept stale readiness.
    if os.path.exists(entry["control_path"]):
        os.unlink(entry["control_path"])
    entry["process"] = await _spawn(entry["config"], entry["control_path"])
    if not await common._wait_ready(entry["process"], entry["config"], entry["control_path"]):
        raise RuntimeError("SSH process exited")


async def _supervise(entry, stop):
    while not stop.is_set():
        if entry["process"].returncode is None:
            await asyncio.sleep(common._SUPERVISOR_POLL)
            continue
        logger.warning("Database tunnel exited; reconnecting")
        delay = common.RESTART_DELAY
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), delay)
                return
            except TimeoutError:
                pass
            entry["process"] = None
            try:
                await _start(entry)
                logger.info("Database tunnel re-established")
                break
            except (OSError, RuntimeError, TimeoutError):
                logger.warning("Database tunnel restart failed; retrying later")
                if entry["process"] is not None:
                    await common._stop(entry["process"], entry["control_path"], entry["config"]["host"])
                delay = common.RESTART_BACKOFF


@asynccontextmanager
async def db_tunnel(settings):
    try:
        env = {**dotenv_values(".env"), **os.environ}
    except Exception:
        raise RuntimeError("Cannot read database tunnel configuration") from None
    config = _config(env, settings)
    if config is None:
        yield
        return
    if not shutil.which("ssh") or (config["password"] and not shutil.which("sshpass")):
        raise RuntimeError("Database tunnel requires ssh and, for password authentication, sshpass")
    stop = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="cvdbssh-") as directory:
        entry = {"config": config, "control_path": os.path.join(directory, "control"), "process": None}
        supervisor = None
        token = None
        try:
            try:
                await _start(entry)
            except (OSError, RuntimeError, TimeoutError):
                raise RuntimeError("Cannot establish database tunnel; check local port, SSH and trusted known_hosts") from None
            supervisor = asyncio.create_task(_supervise(entry, stop))
            token = tunnel_entry.set(entry)
            yield
        finally:
            if token is not None:
                tunnel_entry.reset(token)
            stop.set()
            if supervisor is not None:
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)
            if entry["process"] is not None:
                await common._stop(entry["process"], entry["control_path"], config["host"])
