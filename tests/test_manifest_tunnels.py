import asyncio
import signal
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import manifest_tunnels as tunnels


@pytest.fixture
def harness(monkeypatch):
    env = {
        "MANIFEST_BGD_TUNNEL_ENABLED": "true",
        "MANIFEST_BGD_TUNNEL_REMOTE_HOST": "192.0.2.10",
        "MANIFEST_BGD_TUNNEL_REMOTE_PORT": "3306",
        "MANIFEST_BGD_DB_READONLY_HOST": "127.0.0.1",
        "MANIFEST_BGD_DB_PORT": "13307",
        "MANIFEST_BGD_SSH_HOST": "ssh.example.test",
        "MANIFEST_BGD_SSH_PORT": "23022",
        "MANIFEST_BGD_SSH_USER": "tunnel",
        "MANIFEST_BGD_SSH_PASSWORD": "private-password-$(not-a-command)",
    }
    monkeypatch.delenv("MANIFEST_BGD_TUNNEL_ENABLED")
    monkeypatch.setattr(tunnels, "dotenv_values", lambda _: env)
    monkeypatch.setattr(tunnels.shutil, "which", lambda name: f"/bin/{name}")
    bind = MagicMock()
    monkeypatch.setattr(tunnels.socket, "socket", bind)
    process = MagicMock(pid=987654, returncode=None)
    process.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(tunnels.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tunnels.os.path, "exists", lambda _: True)
    kill = MagicMock()
    monkeypatch.setattr(tunnels.os, "killpg", kill)
    monkeypatch.setattr(tunnels, "_ready", AsyncMock(return_value=True))
    return env, process, spawn, kill, bind


async def test_supervisor_reconnects_after_unexpected_exit(harness, monkeypatch, caplog):
    env, first, spawn, kill, _ = harness
    monkeypatch.setattr(tunnels, "RESTART_DELAY", 0.01)
    control = MagicMock(returncode=0)
    control.wait = AsyncMock(return_value=0)
    second = MagicMock(pid=111222, returncode=None)
    hang = asyncio.get_running_loop().create_future()
    second.wait = AsyncMock(side_effect=lambda: hang)
    spawn.side_effect = [first, second, control]
    with caplog.at_level("INFO"):
        async with tunnels.manifest_tunnels():
            first.returncode = 255
            for _ in range(200):
                if "Manifest tunnel re-established" in caplog.text:
                    break
                await asyncio.sleep(0.01)
            assert "Manifest tunnel re-established" in caplog.text
    assert [call.args for call in kill.call_args_list] == [(second.pid, signal.SIGTERM)]


async def test_owned_lifecycle_and_password_only_in_env(harness, caplog):
    env, process, spawn, kill, bind = harness
    with caplog.at_level("INFO"):
        async with tunnels.manifest_tunnels():
            kill.assert_not_called()
            assert "BGD tunnel ready" in caplog.text
    args, kwargs = spawn.call_args_list[0]
    assert args[:6] == ("sshpass", "-e", "ssh", "-F", "/dev/null", "-N")
    assert "StrictHostKeyChecking=yes" in args
    assert "ExitOnForwardFailure=yes" in args
    assert "ServerAliveInterval=15" in args and "ServerAliveCountMax=3" in args
    assert "127.0.0.1:13307:192.0.2.10:3306" in args
    password = env["MANIFEST_BGD_SSH_PASSWORD"]
    assert password not in str(args) and password not in caplog.text
    assert kwargs["env"]["SSHPASS"] == password
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert "shell" not in kwargs
    bind.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 13307))
    kill.assert_called_once_with(process.pid, signal.SIGTERM)
    assert "-M" in args and "-S" in args
    assert spawn.call_args_list[1].args[:3] == ("ssh", "-F", "/dev/null")
    assert "exit" in spawn.call_args_list[1].args
    assert spawn.await_count == 2  # BGD start and private control exit; CAS remains off.


async def test_disabled_and_environment_override(harness, monkeypatch):
    _, _, spawn, kill, _ = harness
    monkeypatch.setenv("MANIFEST_BGD_TUNNEL_ENABLED", "false")
    async with tunnels.manifest_tunnels():
        pass
    spawn.assert_not_called()
    kill.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("TUNNEL_ENABLED", "yes"), ("SSH_PASSWORD", ""), ("SSH_HOST", "-evil"),
    ("SSH_USER", "bad user"), ("SSH_PORT", "0"), ("DB_PORT", "65536"),
        ("DB_READONLY_HOST", "192.0.2.10"), ("TUNNEL_REMOTE_HOST", ""),
    ("TUNNEL_REMOTE_PORT", "not-a-port"),
])
async def test_invalid_config_nonfatal_and_sanitized(harness, caplog, key, value):
    env, _, spawn, _, _ = harness
    env[f"MANIFEST_BGD_{key}"] = value
    async with tunnels.manifest_tunnels():
        pass
    spawn.assert_not_called()
    assert "Manifest BGD tunnel unavailable" in caplog.text
    if env["MANIFEST_BGD_SSH_PASSWORD"]:
        assert env["MANIFEST_BGD_SSH_PASSWORD"] not in caplog.text


async def test_occupied_port_never_adopted_or_stopped(harness, caplog):
    _, _, spawn, kill, bind = harness
    bind.return_value.__enter__.return_value.bind.side_effect = OSError("private details")
    async with tunnels.manifest_tunnels():
        pass
    spawn.assert_not_called()
    kill.assert_not_called()
    assert "external listener left untouched" in caplog.text
    assert "private details" not in caplog.text


@pytest.mark.parametrize("failure", ["executable", "spawn", "exit", "timeout"])
async def test_startup_failure_nonfatal_no_retry(harness, monkeypatch, caplog, failure):
    _, process, spawn, kill, _ = harness
    if failure == "executable":
        monkeypatch.setattr(tunnels.shutil, "which", lambda _: None)
    elif failure == "spawn":
        spawn.side_effect = OSError("private-password-secret")
    elif failure == "exit":
        process.returncode = 5
    else:
        monkeypatch.setattr(tunnels, "STARTUP_TIMEOUT", 0.01)
        monkeypatch.setattr(tunnels, "_ready", AsyncMock(return_value=False))
    async with tunnels.manifest_tunnels():
        pass
    assert spawn.await_count == (0 if failure == "executable" else 2 if failure in ("exit", "timeout") else 1)
    assert kill.call_count == (1 if failure == "timeout" else 0)
    assert "tunnel unavailable" in caplog.text
    assert "private-password-secret" not in caplog.text
    assert "tunnel ready" not in caplog.text


async def test_cleanup_when_backend_startup_or_request_lifespan_raises(harness):
    _, process, _, kill, _ = harness
    with pytest.raises(RuntimeError, match="backend failure"):
        async with tunnels.manifest_tunnels():
            raise RuntimeError("backend failure")
    kill.assert_called_once_with(process.pid, signal.SIGTERM)


async def test_dotenv_read_failure_nonfatal_and_sanitized(harness, monkeypatch, caplog):
    _, _, spawn, kill, _ = harness
    monkeypatch.setattr(tunnels, "dotenv_values", MagicMock(side_effect=OSError("private-password")))
    async with tunnels.manifest_tunnels():
        pass
    spawn.assert_not_called()
    kill.assert_not_called()
    assert "could not read dotenv configuration" in caplog.text
    assert "private-password" not in caplog.text


async def test_probe_success_does_not_hide_process_exit(harness, monkeypatch, caplog):
    _, process, _, kill, _ = harness

    async def probe(_):
        process.returncode = 255
        return True

    monkeypatch.setattr(tunnels, "_ready", probe)
    async with tunnels.manifest_tunnels():
        pass
    kill.assert_not_called()  # Already exited; private socket shutdown is still attempted.
    assert "tunnel unavailable" in caplog.text
    assert "tunnel ready" not in caplog.text


async def test_cas_opt_in_is_independent_and_stops_in_reverse_order(harness, monkeypatch):
    env, first, spawn, kill, _ = harness
    env.update({key.replace("_BGD_", "_CAS_"): value for key, value in list(env.items())})
    env["MANIFEST_CAS_DB_PORT"] = "13308"
    monkeypatch.delenv("MANIFEST_CAS_TUNNEL_ENABLED")
    second = MagicMock(pid=987655, returncode=None)
    second.wait = AsyncMock(return_value=0)
    control = MagicMock(returncode=0)
    control.wait = AsyncMock(return_value=0)
    spawn.side_effect = [first, second, control, control]
    async with tunnels.manifest_tunnels():
        assert spawn.await_count == 2
    assert [call.args for call in kill.call_args_list] == [
        (second.pid, signal.SIGTERM), (first.pid, signal.SIGTERM),
    ]


def test_missing_opt_in_does_not_require_configuration():
    assert tunnels._config({}, "BGD") is None
    assert tunnels._config({}, "CAS") is None


async def test_cancelled_startup_cleans_owned_process(harness, monkeypatch):
    _, process, _, kill, _ = harness
    monkeypatch.setattr(tunnels, "_ready", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        async with tunnels.manifest_tunnels():
            pytest.fail("cancelled startup must not yield")
    kill.assert_called_once_with(process.pid, signal.SIGTERM)


async def test_shutdown_escalates_and_reaps(harness):
    _, process, _, kill, _ = harness
    # First wait is the control exit command, followed by TERM and KILL waits.
    process.wait.side_effect = [0, TimeoutError, 0]
    async with tunnels.manifest_tunnels():
        pass
    assert [call.args for call in kill.call_args_list] == [
        (process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL),
    ]
    assert process.wait.await_count == 3


async def test_private_control_exit_stops_ssh_without_group_signal(harness):
    _, process, spawn, kill, _ = harness
    control = MagicMock(returncode=0)

    async def exited():
        process.returncode = 0
        return 0

    control.wait = AsyncMock(side_effect=exited)
    spawn.side_effect = [process, control]
    async with tunnels.manifest_tunnels():
        pass
    start_args = spawn.call_args_list[0].args
    exit_args = spawn.call_args_list[1].args
    assert start_args[start_args.index("-S") + 1] == exit_args[exit_args.index("-S") + 1]
    assert exit_args[-3:] == ("-O", "exit", "ssh.example.test")
    kill.assert_not_called()
    process.wait.assert_awaited_once()


async def test_listener_without_owned_control_socket_is_not_ready(harness, monkeypatch, caplog):
    monkeypatch.setattr(tunnels.os.path, "exists", lambda _: False)
    monkeypatch.setattr(tunnels, "STARTUP_TIMEOUT", 0.01)
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(tunnels, "_ready", probe)
    async with tunnels.manifest_tunnels():
        pass
    probe.assert_not_awaited()
    assert "startup timed out" in caplog.text
    assert "tunnel ready" not in caplog.text


async def test_control_command_failure_falls_back_to_owned_process(harness):
    _, process, spawn, kill, _ = harness
    spawn.side_effect = [process, FileNotFoundError("private-details")]
    async with tunnels.manifest_tunnels():
        pass
    kill.assert_called_once_with(process.pid, signal.SIGTERM)


def test_cas_requires_explicit_target_no_guessed_readonly(harness):
    env = {key.replace("_BGD_", "_CAS_"): value for key, value in harness[0].items()}
    env.pop("MANIFEST_CAS_DB_READONLY_HOST")
    env["MANIFEST_CAS_DB_HOST"] = "localhost"
    env.pop("MANIFEST_CAS_TUNNEL_REMOTE_HOST")
    with pytest.raises(ValueError, match="missing TUNNEL_REMOTE_HOST"):
        tunnels._config(env, "CAS")
    env["MANIFEST_CAS_TUNNEL_REMOTE_HOST"] = "verified.example.test"
    assert tunnels._config(env, "CAS")["remote_host"] == "verified.example.test"


async def test_fastapi_lifespan_wraps_backend_and_always_cleans(tmp_path, monkeypatch):
    import app.main as main
    from app.config import Settings

    events = []

    @asynccontextmanager
    async def mocked_tunnels():
        events.append("start")
        try:
            yield
        finally:
            events.append("stop")

    def fail_embedder(_):
        events.append("backend")
        raise RuntimeError("backend failure")

    monkeypatch.setattr(main, "manifest_tunnels", mocked_tunnels)
    monkeypatch.setattr(main, "create_embedder", fail_embedder)
    app = main.create_app(Settings(_env_file=None, jwt_secret="x" * 32,
                                   bootstrap_password="test", crop_dir=tmp_path / "crops"))
    with pytest.raises(RuntimeError, match="backend failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("backend startup failed")
    assert events == ["start", "backend", "stop"]
