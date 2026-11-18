import os
import subprocess

import pytest

from tools import photo_sync


@pytest.fixture
def setup_sync(tmp_path, monkeypatch):
    root = tmp_path / "crops"
    (root / "cas" / "batch").mkdir(parents=True)
    key = tmp_path / "key"
    known = tmp_path / "known_hosts"
    key.touch()
    known.touch()
    env = {"PHOTO_SYNC_SOURCE": str(root), "PHOTO_SYNC_KEY": str(key),
           "PHOTO_SYNC_KNOWN_HOSTS": str(known), "PHOTO_SYNC_USER": "sync",
           "PHOTO_SYNC_HOST": "example.test", "PHOTO_SYNC_DEST": "/srv/photos"}
    return root, env


def test_file_selection_and_fixed_command(setup_sync, tmp_path, monkeypatch):
    root, env = setup_sync
    batch = root / "cas" / "batch"
    (batch / "one.PNG").write_bytes(b"one")
    (batch / "two.jpeg").write_bytes(b"two")
    (batch / ".hidden.jpg").touch()
    (batch / "upload.tmp.png").touch()
    (batch / "link.jpg").symlink_to(batch / "one.PNG")
    (root / "cas" / "link").symlink_to(batch, target_is_directory=True)
    (root / "cas" / ".hidden").mkdir()
    (root / "cas" / ".hidden" / "x.jpg").touch()
    (root / "other").mkdir()
    (root / "other" / "x.jpg").touch()
    (root / "legacy.png").touch()
    root, cmd = photo_sync.config(env)
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
    monkeypatch.setattr(photo_sync.subprocess, "run", fake_run)
    photo_sync.sync_once(root, cmd, 42)
    command, options = calls[0]
    assert set(options["input"].split(b"\0")) == {b"cas/batch/one.PNG", b"cas/batch/two.jpeg", b""}
    assert {"--checksum", "--no-links", "--from0", "--files-from=-"} <= set(command)
    assert not any("delete" in arg or arg in {"--inplace", "--ignore-existing"} for arg in command)
    assert command[-1] == "sync@example.test:/srv/photos/"
    assert "StrictHostKeyChecking=yes" in command[-3]
    assert options["stdout"] == options["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize("field,value", [("PHOTO_SYNC_DEST", "/srv/../etc"),
                                       ("PHOTO_SYNC_DEST", "/srv/x;touch"),
                                       ("PHOTO_SYNC_HOST", "-bad"),
                                       ("PHOTO_SYNC_USER", "bad@host")])
def test_unsafe_remote_rejected(setup_sync, field, value):
    _, env = setup_sync
    env[field] = value
    with pytest.raises(ValueError):
        photo_sync.config(env)


def test_source_and_tenant_symlinks_rejected(setup_sync, tmp_path):
    root, env = setup_sync
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    env["PHOTO_SYNC_SOURCE"] = str(link)
    with pytest.raises(ValueError):
        photo_sync.config(env)
    (root / "cas").rename(root / "moved")
    (root / "cas").symlink_to(root / "moved", target_is_directory=True)
    with pytest.raises(ValueError):
        photo_sync.photos(root)


def test_failure_can_retry(setup_sync, monkeypatch):
    root, env = setup_sync
    (root / "cas" / "batch" / "x.jpg").touch()
    _, cmd = photo_sync.config(env)
    attempts = []
    def fake_run(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(1, "rsync")
    monkeypatch.setattr(photo_sync.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        photo_sync.sync_once(root, cmd, 42)
    photo_sync.sync_once(root, cmd, 42)
    assert len(attempts) == 2


def test_fake_rsync_receives_file_list(setup_sync, tmp_path, monkeypatch):
    root, env = setup_sync
    (root / "cas" / "batch" / "x.png").touch()
    fake = tmp_path / "rsync"
    output = tmp_path / "listing"
    fake.write_text("#!/bin/sh\n/bin/cat > \"$PHOTO_TEST_OUTPUT\"\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PHOTO_TEST_OUTPUT", str(output))
    _, command = photo_sync.config(env)
    with open(os.devnull, "rb") as lock:
        photo_sync.sync_once(root, command, lock.fileno())
    assert output.read_bytes() == b"cas/batch/x.png\0"


def test_system_rsync_updates_without_deleting(setup_sync, tmp_path):
    root, _ = setup_sync
    source = root / "cas" / "batch" / "x.jpg"
    source.write_bytes(b"first")
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.jpg").write_bytes(b"keep")
    cmd = ["rsync", "--checksum", "--no-links", "--files-from=-", "--from0", str(root) + "/", str(target) + "/"]
    with open(os.devnull, "rb") as lock:
        photo_sync.sync_once(root, cmd, lock.fileno())
        source.write_bytes(b"other")
        photo_sync.sync_once(root, cmd, lock.fileno())
    assert (target / "cas" / "batch" / "x.jpg").read_bytes() == b"other"
    assert (target / "keep.jpg").read_bytes() == b"keep"
