import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import db_tunnel as tunnel
from app import db
from app.config import Settings


@pytest.fixture
def setup(monkeypatch):
    env = {
        "CV_DB_TUNNEL_ENABLED": "true",
        "CV_DB_TUNNEL_SSH_HOST": "ssh.example.test",
        "CV_DB_TUNNEL_SSH_USER": "tunnel",
        "CV_DB_TUNNEL_SSH_PORT": "22",
    }
    monkeypatch.delenv("CV_DB_TUNNEL_ENABLED")
    monkeypatch.setattr(tunnel, "dotenv_values", lambda _: env)
    monkeypatch.setattr(tunnel.shutil, "which", lambda _: "/bin/ssh")
    monkeypatch.setattr(tunnel, "_check_port", MagicMock())
    monkeypatch.setattr(tunnel.common, "_wait_ready", AsyncMock(return_value=True))
    stop = AsyncMock()
    monkeypatch.setattr(tunnel.common, "_stop", stop)
    process = MagicMock(returncode=None)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(tunnel.asyncio, "create_subprocess_exec", spawn)
    settings = Settings(_env_file=None, database_url="postgresql://user:secret@127.0.0.1:15432/db")
    monkeypatch.delenv("CV_DATABASE_URL", raising=False)
    return env, settings, spawn, process, stop


async def test_disabled(setup, monkeypatch):
    env, settings, spawn, _, _ = setup
    monkeypatch.setenv("CV_DB_TUNNEL_ENABLED", "false")
    async with tunnel.db_tunnel(settings):
        pass
    spawn.assert_not_awaited()


@pytest.mark.parametrize("key,value", [
    ("CV_DB_TUNNEL_ENABLED", "yes"), ("CV_DB_TUNNEL_SSH_HOST", "-bad"),
    ("CV_DB_TUNNEL_SSH_USER", "bad user"), ("CV_DB_TUNNEL_SSH_PORT", "65536"),
])
async def test_invalid_config(setup, key, value):
    env, settings, spawn, _, _ = setup
    env[key] = value
    with pytest.raises(ValueError):
        async with tunnel.db_tunnel(settings):
            pass
    spawn.assert_not_awaited()


@pytest.mark.parametrize("value", ["relative/key", "/missing/key", "/tmp/key\n-oProxyCommand=bad", "/tmp/%h"])
async def test_invalid_key(setup, value, tmp_path):
    env, settings, spawn, _, _ = setup
    env["CV_DB_TUNNEL_SSH_KEY"] = value
    with pytest.raises(ValueError, match="CV_DB_TUNNEL_SSH_KEY"):
        async with tunnel.db_tunnel(settings):
            pass
    env["CV_DB_TUNNEL_SSH_KEY"] = str(tmp_path)
    with pytest.raises(ValueError, match="CV_DB_TUNNEL_SSH_KEY"):
        tunnel._config(env, settings)
    spawn.assert_not_awaited()


@pytest.mark.parametrize("password", [None, "private password"])
async def test_explicit_key(setup, tmp_path, password):
    env, settings, spawn, _, _ = setup
    key = tmp_path / "private key"
    key.touch()
    env["CV_DB_TUNNEL_SSH_KEY"] = str(key)
    if password:
        env["CV_DB_TUNNEL_SSH_PASSWORD"] = password
    async with tunnel.db_tunnel(settings):
        args, kwargs = spawn.call_args
        assert args[args.index("-i") + 1] == str(key)
        assert "IdentitiesOnly=yes" in args
        assert "BatchMode=yes" in args
        assert "StrictHostKeyChecking=yes" in args
        assert args[:3] == (("sshpass", "-e", "ssh") if password else ("ssh", "-F", "/dev/null"))
        assert kwargs["env"].get("SSHPASS") == password


@pytest.mark.parametrize("dsn", [
    "postgresql://localhost:15432/db", "postgresql://127.0.0.1:5432/db",
    "postgresql://127.0.0.1:15432/db?host=evil", "host=127.0.0.1 port=15432",
    "postgresql://127.0.0.1:15432/db?port=5432",
])
def test_wrong_dsn(setup, dsn):
    env, settings, _, _, _ = setup
    settings.database_url = dsn
    with pytest.raises(ValueError):
        tunnel._config(env, settings)


async def test_port_conflict_fails_closed(setup, monkeypatch):
    _, settings, spawn, _, stop = setup
    monkeypatch.setattr(tunnel, "_check_port", MagicMock(side_effect=OSError("secret")))
    with pytest.raises(RuntimeError, match="Cannot establish database tunnel"):
        async with tunnel.db_tunnel(settings):
            pytest.fail("must not start backend")
    spawn.assert_not_awaited()
    stop.assert_not_awaited()


@pytest.mark.parametrize("password", [None, "private password"])
async def test_lifecycle_and_auth(setup, password, caplog):
    env, settings, spawn, process, stop = setup
    if password:
        env["CV_DB_TUNNEL_SSH_PASSWORD"] = password
    with pytest.raises(RuntimeError, match="backend failure"):
        async with tunnel.db_tunnel(settings):
            args, kwargs = spawn.call_args
            assert args[:3] == (("sshpass", "-e", "ssh") if password else ("ssh", "-F", "/dev/null"))
            assert "StrictHostKeyChecking=yes" in args
            assert "ExitOnForwardFailure=yes" in args
            assert "127.0.0.1:15432:127.0.0.1:5432" in args
            if password:
                assert password not in str(args) and password not in caplog.text
                assert kwargs["env"]["SSHPASS"] == password
            else:
                assert "SSHPASS" not in kwargs["env"]
            raise RuntimeError("backend failure")
    stop.assert_awaited_once()
    assert stop.call_args.args[0] is process


async def test_exited_process_and_retry(setup, monkeypatch):
    _, settings, spawn, first, stop = setup
    second = MagicMock(returncode=None)
    spawn.side_effect = [first, OSError("private"), second]
    monkeypatch.setattr(tunnel.common, "_SUPERVISOR_POLL", 0.001)
    monkeypatch.setattr(tunnel.common, "RESTART_DELAY", 0.001)
    monkeypatch.setattr(tunnel.common, "RESTART_BACKOFF", 0.001)
    async with tunnel.db_tunnel(settings):
        first.returncode = 255
        for _ in range(100):
            if spawn.await_count == 3:
                break
            await asyncio.sleep(0.005)
        assert spawn.await_count == 3
    assert stop.call_args.args[0] is second


async def test_startup_process_exit_fails(setup, monkeypatch):
    _, settings, _, process, stop = setup
    monkeypatch.setattr(tunnel.common, "_wait_ready", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="Cannot establish"):
        async with tunnel.db_tunnel(settings):
            pytest.fail("must not start backend")
    assert stop.call_args.args[0] is process


async def test_pool_connect_rejects_dead_or_replaced_tunnel(setup, monkeypatch):
    _, settings, _, process, _ = setup
    entry = {"process": process}
    token = db.tunnel_entry.set(entry)
    captured = {}

    async def create_pool(*args, **kwargs):
        captured["connect"] = kwargs["connect"]
        raise OSError("stop before database access")

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    try:
        with pytest.raises(db.DatabaseUnavailableError):
            await db.create_pool(settings)
    finally:
        db.tunnel_entry.reset(token)
    connect = captured["connect"]
    process.returncode = 255
    with pytest.raises(db.DatabaseUnavailableError):
        await connect()
    process.returncode = None
    connection = MagicMock(close=AsyncMock())

    async def replace(*args, **kwargs):
        entry["process"] = MagicMock(returncode=None)
        return connection

    monkeypatch.setattr(db.asyncpg, "connect", replace)
    with pytest.raises(db.DatabaseUnavailableError):
        await connect()
    connection.close.assert_awaited_once()
