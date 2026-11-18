#!/usr/bin/env python3
"""Standalone, one-way reconciliation of the locally owned CAS photos."""

import fcntl
import hashlib
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LOG = logging.getLogger("photo-sync")
SAFE_REMOTE_PATH = re.compile(r"/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")
SAFE_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
SAFE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")


def local_path(value, directory=False):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("local paths must be absolute and contain no '..'")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink in local path")
    if directory and not path.is_dir():
        raise ValueError("source is not a directory")
    if not directory and not path.is_file():
        raise ValueError("SSH configuration is not a regular file")
    return path


def config(env):
    root = local_path(env["PHOTO_SYNC_SOURCE"], directory=True)
    local_path(str(root / "cas"), directory=True)
    key = local_path(env["PHOTO_SYNC_KEY"])
    known_hosts = local_path(env["PHOTO_SYNC_KNOWN_HOSTS"])
    user, host, dest = (env[name] for name in ("PHOTO_SYNC_USER", "PHOTO_SYNC_HOST", "PHOTO_SYNC_DEST"))
    if not SAFE_USER.fullmatch(user) or not SAFE_HOST.fullmatch(host) or not SAFE_REMOTE_PATH.fullmatch(dest):
        raise ValueError("invalid remote user, host, or absolute destination")
    port = int(env.get("PHOTO_SYNC_PORT", "22"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid SSH port")
    ssh = ["ssh", "-F", "/dev/null", "-i", str(key), "-p", str(port),
           "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
           "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
           "-o", "ProxyCommand=none", "-o", "ConnectTimeout=10"]
    return root, ["rsync", "--checksum", "--no-links", "--files-from=-", "--from0",
                  "-e", shlex.join(ssh), str(root) + "/", f"{user}@{host}:{dest}/"]


def photos(root):
    # Never follow directory symlinks, including a replaced tenant root.
    local_path(str(root / "cas"), directory=True)
    result = []
    def on_error(_error):
        raise OSError("cannot scan source")
    for base, dirs, files in os.walk(root / "cas", followlinks=False, onerror=on_error):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith(("~", ".tmp", ".part"))
                   and not (Path(base) / d).is_symlink()]
        for name in files:
            path = Path(base) / name
            if (name.startswith(".") or name.endswith("~") or
                    any(marker in name.lower() for marker in (".tmp.", ".temp.", ".part.")) or
                    path.suffix.lower() not in {".png", ".jpg", ".jpeg"}):
                continue
            if stat.S_ISREG(path.lstat().st_mode):
                result.append(os.fsencode(str(path.relative_to(root))) + b"\0")
    return b"".join(result)


def sync_once(root, command, lock_fd):
    listing = photos(root)
    if not listing:
        return
    # Discard subprocess output: paths, remote errors, and SSH diagnostics may be sensitive.
    subprocess.run(command, input=listing, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=120, pass_fds=(lock_fd,))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        root, command = config(os.environ)
        digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:24]
        lock_path = Path(tempfile.gettempdir()) / f"photo-sync-{os.getuid()}-{digest}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("another sync instance is running")
            return 1
    except (KeyError, ValueError, OSError):
        LOG.error("invalid or inaccessible sync configuration")
        return 1
    LOG.info("photo sync started")
    while True:
        try:
            sync_once(root, command, fd)
        except (OSError, ValueError, subprocess.SubprocessError):
            LOG.warning("reconciliation failed; retrying")
        time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
