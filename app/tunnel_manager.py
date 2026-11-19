"""Cross-platform, application-owned SSH forwarding workers."""

import logging
import os
import select
import socket
import threading
import time

import paramiko

logger = logging.getLogger(__name__)
PROBE_INTERVAL = 3.0
PROBE_TIMEOUT = 3.0


class _Connection:
    def __init__(self, transport, listener):
        self.transport = transport
        self.listener = listener

    @property
    def returncode(self):
        return None if self.transport.is_active() and self.listener.fileno() >= 0 else 1


class _Tunnel:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.stop_event = None
        self.client = None
        self.listener = None
        self.entry = {"process": None}
        self.state = "stopped"

    def start(self):
        with self.lock:
            if self.stop_event is not None:
                return
            stop = threading.Event()
            self.stop_event = stop
            self.state = "connecting"
            threading.Thread(target=self._run, args=(stop,), daemon=True).start()

    def stop(self):
        with self.lock:
            stop = self.stop_event
            self.stop_event = None
            self.state = "stopped"
            self.entry["process"] = None
            client, listener = self.client, self.listener
            self.client = self.listener = None
            if stop:
                stop.set()
            if listener:
                listener.close()
        if client:
            client.close()

    def status(self):
        with self.lock:
            process = self.entry["process"]
            alive = process is not None and process.returncode is None
            state = self.state
            if state == "connected" and not alive:
                state = "connecting"
            return {"enabled": True, "state": state, "alive": alive}

    def _run(self, stop):
        while not stop.is_set():
            client = paramiko.SSHClient()
            listener = None
            try:
                client.load_system_host_keys()
                known_hosts = self.config.get("known_hosts")
                if known_hosts:
                    client.load_host_keys(os.path.expanduser(known_hosts))
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
                with self.lock:
                    if self.stop_event is not stop:
                        return
                    self.client = client
                client.connect(
                    hostname=self.config["host"], port=self.config["port"],
                    username=self.config["user"], password=self.config.get("password"),
                    key_filename=self.config.get("key"), timeout=5, banner_timeout=5,
                    auth_timeout=5, look_for_keys=not bool(self.config.get("password") or self.config.get("key")),
                    allow_agent=not bool(self.config.get("password") or self.config.get("key")),
                )
                transport = client.get_transport()
                if transport is None or not transport.is_active():
                    raise OSError("SSH transport unavailable")
                transport.set_keepalive(5)
                self._probe(transport)
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.bind(("127.0.0.1", self.config["local_port"]))
                listener.listen(32)
                listener.settimeout(0.2)
                with self.lock:
                    if self.stop_event is not stop:
                        return
                    self.listener = listener
                    self.entry["process"] = _Connection(transport, listener)
                    self.state = "connected"
                next_probe = time.monotonic() + PROBE_INTERVAL
                while not stop.is_set() and transport.is_active():
                    if time.monotonic() >= next_probe:
                        self._probe(transport)
                        next_probe = time.monotonic() + PROBE_INTERVAL
                    try:
                        sock, address = listener.accept()
                    except TimeoutError:
                        continue
                    threading.Thread(target=self._forward, args=(sock, address, transport, stop), daemon=True).start()
            except Exception:
                if not stop.is_set():
                    logger.warning("SSH tunnel unavailable; retrying")
            finally:
                with self.lock:
                    if self.stop_event is stop:
                        self.entry["process"] = None
                        self.client = self.listener = None
                        self.state = "connecting"
                    if listener:
                        listener.close()
                client.close()
            stop.wait(1)

    def _probe(self, transport):
        """Detect a dead path even when TCP still considers SSH connected."""
        channel = transport.open_channel(
            "direct-tcpip", (self.config["remote_host"], self.config["remote_port"]),
            ("127.0.0.1", 0), timeout=PROBE_TIMEOUT,
        )
        if channel is None:
            raise OSError("SSH forwarding unavailable")
        channel.close()

    def _forward(self, sock, address, transport, stop):
        channel = None
        try:
            channel = transport.open_channel(
                "direct-tcpip", (self.config["remote_host"], self.config["remote_port"]), address,
                timeout=5,
            )
            if channel is None:
                return
            while not stop.is_set() and transport.is_active():
                ready, _, _ = select.select([sock, channel], [], [], 0.2)
                for source in ready:
                    data = source.recv(32768)
                    if not data:
                        return
                    (channel if source is sock else sock).sendall(data)
        except Exception:
            pass
        finally:
            sock.close()
            if channel:
                channel.close()


class TunnelManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.tunnels = {}

    def configure(self, name, config):
        with self.lock:
            old = self.tunnels.pop(name, None)
            if old:
                old.stop()
            if config is not None:
                self.tunnels[name] = _Tunnel(config)
                return self.tunnels[name].entry
            return None

    def start(self, name):
        with self.lock:
            if name in self.tunnels:
                self.tunnels[name].start()

    def stop(self, name):
        with self.lock:
            if name in self.tunnels:
                self.tunnels[name].stop()

    def restart(self, name):
        with self.lock:
            self.stop(name)
            self.start(name)

    def status(self, name):
        if name not in ("db", "bgd", "cas"):
            raise ValueError("unknown tunnel")
        with self.lock:
            tunnel = self.tunnels.get(name)
            return tunnel.status() if tunnel else {"enabled": False, "state": "disabled", "alive": False}

    def statuses(self):
        return {name: self.status(name) for name in ("db", "bgd", "cas")}

    def open_socket(self, name, timeout=5):
        """Connect to the owned listener while preventing a concurrent stop/rebind."""
        with self.lock:
            tunnel = self.tunnels.get(name)
            if tunnel is None:
                raise OSError("Tunnel is not configured")
            with tunnel.lock:
                process = tunnel.entry["process"]
                if process is None or process.returncode is not None:
                    raise OSError("Tunnel is unavailable")
                return socket.create_connection(process.listener.getsockname(), timeout=timeout)


manager = TunnelManager()
