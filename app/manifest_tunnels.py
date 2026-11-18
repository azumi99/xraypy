"""Opt-in, lifespan-owned SSH forwards for manifest lookups."""

import asyncio
import logging
import os
import re
import shutil
import signal
import socket
import tempfile
from contextlib import asynccontextmanager

from dotenv import dotenv_values

logger = logging.getLogger(__name__)
STARTUP_TIMEOUT = 10.0
STOP_TIMEOUT = 3.0
RESTART_DELAY = 5.0
RESTART_BACKOFF = 30.0
_SUPERVISOR_POLL = 0.05


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
    return {
        "host": host("SSH_HOST"), "port": port("SSH_PORT"), "user": user,
        "password": required("SSH_PASSWORD"), "local_port": port("DB_PORT"),
        "remote_host": host("TUNNEL_REMOTE_HOST"), "remote_port": port("TUNNEL_REMOTE_PORT"),
    }


async def _ready(port):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.2)
    except (OSError, TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def _spawn(config, control_path):
    child_env = {**os.environ, "SSHPASS": config["password"]}
    return await asyncio.create_subprocess_exec(
        "sshpass", "-e", "ssh", "-F", "/dev/null", "-N", "-T",
        "-M", "-S", control_path,
        "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
        "-o", "NumberOfPasswordPrompts=1",
        "-L", f"127.0.0.1:{config['local_port']}:{config['remote_host']}:{config['remote_port']}",
        "-p", str(config["port"]), "-l", config["user"], config["host"],
        env=child_env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


async def _wait_ready(process, config, control_path):
    """True saat forward siap; False bila proses mati; TimeoutError bila lambat."""
    deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT
    while process.returncode is None:
        if os.path.exists(control_path) and await _ready(config["local_port"]):
            # Let bind/authentication failures reach the process watcher first.
            await asyncio.sleep(0.1)
            if process.returncode is None:
                return True
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.1)
    return False


async def _supervise(entry, stop):
    # ponytail: poll returncode; process.wait() dipakai jalur shutdown agar tak mengganggunya.
    while not stop.is_set():
        if entry["process"].returncode is None:
            await asyncio.sleep(_SUPERVISOR_POLL)
            continue
        logger.warning("Manifest tunnel exited (code %s); reconnecting", entry["process"].returncode)
        delay = RESTART_DELAY
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), delay)
                return
            except TimeoutError:
                pass
            try:
                entry["process"] = await _spawn(entry["config"], entry["control_path"])
            except OSError:
                logger.warning("Manifest tunnel restart failed; retrying later")
                delay = RESTART_BACKOFF
                continue
            try:
                if await _wait_ready(entry["process"], entry["config"], entry["control_path"]):
                    logger.info("Manifest tunnel re-established")
                    break
            except TimeoutError:
                pass
            await _stop(entry["process"], entry["control_path"], entry["host"])
            delay = RESTART_BACKOFF


async def _stop(process, control_path, host):
    # sshpass's ssh child calls setsid(); use its private control socket first.
    control = None
    try:
        control = await asyncio.create_subprocess_exec(
            "ssh", "-F", "/dev/null", "-S", control_path, "-O", "exit", host,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(control.wait(), STOP_TIMEOUT)
    except (OSError, TimeoutError):
        if control is not None and control.returncode is None:
            try:
                control.kill()
                await asyncio.wait_for(control.wait(), STOP_TIMEOUT)
            except (ProcessLookupError, TimeoutError):
                logger.warning("Manifest tunnel control command did not exit cleanly")
    # Closing sshpass's PTY also hangs up SSH if startup never made a socket.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if process.returncode is not None:
            await process.wait()
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
            return
        except TimeoutError:
            continue
    logger.warning("Manifest tunnel process did not exit after SIGKILL")


@asynccontextmanager
async def manifest_tunnels():
    entries = []
    supervisors = []
    stop = asyncio.Event()
    control_dir = None
    try:
        try:
            env = {**dotenv_values(".env"), **os.environ}
        except Exception:
            logger.warning("Manifest tunnels unavailable: could not read dotenv configuration")
            yield
            return
        for warehouse in ("BGD", "CAS"):
            entry = None
            try:
                config = _config(env, warehouse)
            except ValueError as exc:
                # Validation errors contain field names only, never supplied values.
                logger.warning("Manifest %s tunnel unavailable: %s", warehouse, exc)
                continue
            try:
                if config is None:
                    continue
                if not shutil.which("sshpass") or not shutil.which("ssh"):
                    logger.warning("Manifest %s tunnel unavailable: missing sshpass or ssh", warehouse)
                    continue
                # Never adopt an external listener, even if it is a working tunnel.
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    try:
                        probe.bind(("127.0.0.1", config["local_port"]))
                    except OSError:
                        logger.warning("Manifest %s tunnel unavailable: local bind port occupied or unavailable; external listener left untouched", warehouse)
                        continue
                if control_dir is None:
                    control_dir = tempfile.TemporaryDirectory(prefix="cvssh-")
                control_path = os.path.join(control_dir.name, warehouse.lower())
                entry = {"process": None, "control_path": control_path, "host": config["host"], "config": config}
                entry["process"] = await _spawn(config, control_path)
                entries.append(entry)
                if not await _wait_ready(entry["process"], config, control_path):
                    raise RuntimeError("process exited")
                logger.info("Manifest %s tunnel ready (owned local forward)", warehouse)
                supervisors.append(asyncio.create_task(_supervise(entry, stop)))
            except Exception as exc:
                reason = "startup timed out" if isinstance(exc, TimeoutError) else "SSH startup failed"
                logger.warning("Manifest %s tunnel unavailable: %s; check executables, credentials, reachability and trusted known_hosts", warehouse, reason)
                if entry is not None and entry["process"] is not None:
                    await _stop(entry["process"], entry["control_path"], entry["host"])
                    entries.remove(entry)
        yield
    finally:
        stop.set()
        for task in supervisors:
            task.cancel()
        if supervisors:
            await asyncio.gather(*supervisors, return_exceptions=True)
        try:
            for entry in reversed(entries):
                await _stop(entry["process"], entry["control_path"], entry["host"])
        finally:
            if control_dir is not None:
                control_dir.cleanup()
