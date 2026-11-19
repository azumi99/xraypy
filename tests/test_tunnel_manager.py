import socket
import threading
import time
from unittest.mock import MagicMock

import paramiko
import pytest

from app import tunnel_manager


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def config(port):
    return {"host": "example.test", "port": 22, "user": "test", "password": "secret",
            "local_port": port, "remote_host": "127.0.0.1", "remote_port": 5432}


def test_reconnect_owned_listener_and_controls(monkeypatch, caplog):
    clients = []

    def make_client():
        client = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = True
        client.get_transport.return_value = transport
        clients.append(client)
        return client

    monkeypatch.setattr(tunnel_manager.paramiko, "SSHClient", make_client)
    manager = tunnel_manager.TunnelManager()
    entry = manager.configure("bgd", config(0))
    manager.start("bgd")
    assert wait_for(lambda: manager.status("bgd")["alive"])
    first = entry["process"]
    assert isinstance(clients[0].set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
    assert clients[0].connect.call_args.kwargs["password"] == "secret"
    with manager.open_socket("bgd") as sock:
        assert sock.getpeername()[0] == "127.0.0.1"
    clients[0].get_transport().is_active.return_value = False
    assert wait_for(lambda: entry["process"] is not None and entry["process"] is not first)
    assert first.returncode is not None
    manager.stop("bgd")
    assert not manager.status("bgd")["alive"]
    with pytest.raises(OSError):
        manager.open_socket("bgd")
    manager.restart("bgd")
    assert wait_for(lambda: manager.status("bgd")["alive"])
    manager.configure("bgd", None)
    assert "secret" not in str(manager.statuses()) + caplog.text


def test_occupied_port_never_adopted(monkeypatch):
    client = MagicMock()
    client.get_transport().is_active.return_value = True
    monkeypatch.setattr(tunnel_manager.paramiko, "SSHClient", lambda: client)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        manager = tunnel_manager.TunnelManager()
        entry = manager.configure("cas", config(listener.getsockname()[1]))
        manager.start("cas")
        assert wait_for(lambda: client.connect.called)
        time.sleep(0.1)
        assert entry["process"] is None
        assert not manager.status("cas")["alive"]
        manager.stop("cas")
        assert listener.fileno() >= 0


def test_stop_during_slow_connect_cannot_publish(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    client = MagicMock()
    client.get_transport().is_active.return_value = True

    def connect(**kwargs):
        entered.set()
        release.wait(3)

    client.connect.side_effect = connect
    monkeypatch.setattr(tunnel_manager.paramiko, "SSHClient", lambda: client)
    manager = tunnel_manager.TunnelManager()
    entry = manager.configure("db", config(0))
    manager.start("db")
    assert entered.wait(1)
    manager.stop("db")
    release.set()
    assert wait_for(lambda: client.close.call_count >= 2)
    assert entry["process"] is None
    assert manager.status("db")["state"] == "stopped"


def test_forward_probe_failure_reconnects(monkeypatch):
    clients = []

    def make_client():
        client = MagicMock()
        transport = client.get_transport.return_value
        transport.is_active.return_value = True
        if not clients:
            transport.open_channel.side_effect = OSError("remote forwarding failed")
        clients.append(client)
        return client

    monkeypatch.setattr(tunnel_manager.paramiko, "SSHClient", make_client)
    manager = tunnel_manager.TunnelManager()
    manager.configure("bgd", config(0))
    manager.start("bgd")
    try:
        assert wait_for(lambda: len(clients) >= 2 and manager.status("bgd")["alive"], timeout=4)
        assert clients[0].close.called
        clients[1].get_transport().open_channel.assert_called()
    finally:
        manager.configure("bgd", None)
