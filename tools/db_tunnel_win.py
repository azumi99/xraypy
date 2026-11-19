"""SSH tunnels (paramiko) for cv-service on Windows.

Forwards:
  1. DB:   127.0.0.1:15432 -> 127.0.0.1:5432  via 159.138.103.200:23022
  2. CAS:  127.0.0.1:13308 -> 10.1.1.113:3306  via 110.239.67.228:23022
  3. BGD:  127.0.0.1:13307 -> 10.1.1.103:3306  via 110.239.92.41:23022

Each tunnel auto-reconnects if the SSH session drops.
"""
import socket
import threading
import select
import sys
import time

import paramiko

TUNNELS = [
    {
        "name": "DB",
        "ssh_host": "159.138.103.200", "ssh_port": 23022,
        "ssh_user": "root", "ssh_pass": "hid_lywyyd5tp0_4stw",
        "local": ("127.0.0.1", 15432), "remote": ("127.0.0.1", 5432),
    },
    {
        "name": "CAS",
        "ssh_host": "110.239.67.228", "ssh_port": 23022,
        "ssh_user": "root", "ssh_pass": "/MF6:rS6T+h{",
        "local": ("127.0.0.1", 13308), "remote": ("10.1.1.113", 3306),
    },
    {
        "name": "BGD",
        "ssh_host": "110.239.92.41", "ssh_port": 23022,
        "ssh_user": "ipung", "ssh_pass": "GtteVdfr+DEjgNz&",
        "local": ("127.0.0.1", 13307), "remote": ("10.1.1.103", 3306),
    },
]


def forward(chan, sock):
    try:
        while True:
            r, _, _ = select.select([sock, chan], [], [])
            if sock in r:
                data = sock.recv(4096)
                if not data:
                    break
                chan.send(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                sock.send(data)
    except Exception:
        pass
    finally:
        try: chan.close()
        except Exception: pass
        try: sock.close()
        except Exception: pass


def connect_ssh(cfg):
    transport = paramiko.Transport((cfg["ssh_host"], cfg["ssh_port"]))
    transport.use_compression = True
    transport.set_keepalive(30)
    transport.start_client()
    try:
        transport.auth_password(cfg["ssh_user"], cfg["ssh_pass"])
    except paramiko.SSHException:
        transport.auth_interactive(
            cfg["ssh_user"], lambda title, instructions, prompts: [cfg["ssh_pass"]] * len(prompts)
        )
    return transport


def serve_tunnel(cfg):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(cfg["local"])
    srv.listen(5)
    transport = None
    while True:
        try:
            if transport is None or not transport.is_active():
                if transport is not None:
                    try: transport.close()
                    except Exception: pass
                transport = connect_ssh(cfg)
                print(
                    f"[{cfg['name']}] listening {cfg['local'][0]}:{cfg['local'][1]} "
                    f"-> {cfg['remote'][0]}:{cfg['remote'][1]} via {cfg['ssh_host']}:{cfg['ssh_port']}",
                    flush=True,
                )
            sock, _ = srv.accept()
            if not transport.is_active():
                try: transport.close()
                except Exception: pass
                transport = connect_ssh(cfg)
                print(f"[{cfg['name']}] reconnected", flush=True)
            chan = transport.open_channel("direct-tcpip", cfg["remote"], sock.getpeername())
            if chan is None:
                sock.close()
                continue
            t = threading.Thread(target=forward, args=(chan, sock), daemon=True)
            t.start()
        except Exception as e:
            print(f"[{cfg['name']}] error: {e}; reconnecting in 5s", flush=True)
            if transport is not None:
                try: transport.close()
                except Exception: pass
                transport = None
            time.sleep(5)


def main():
    for cfg in TUNNELS:
        t = threading.Thread(target=serve_tunnel, args=(cfg,), daemon=True)
        t.start()
    print("all tunnels started", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
