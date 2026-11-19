import pytest

from app import manifest_tunnels


@pytest.fixture
def env():
    return {"MANIFEST_BGD_TUNNEL_ENABLED": "true", "MANIFEST_BGD_TUNNEL_REMOTE_HOST": "192.0.2.10",
            "MANIFEST_BGD_TUNNEL_REMOTE_PORT": "3306", "MANIFEST_BGD_DB_READONLY_HOST": "127.0.0.1",
            "MANIFEST_BGD_DB_PORT": "13307", "MANIFEST_BGD_SSH_HOST": "ssh.example.test",
            "MANIFEST_BGD_SSH_PORT": "23022", "MANIFEST_BGD_SSH_USER": "tunnel",
            "MANIFEST_BGD_SSH_PASSWORD": "private-password"}


@pytest.mark.parametrize("key,value", [("TUNNEL_ENABLED", "yes"), ("SSH_PASSWORD", ""),
    ("SSH_HOST", "-evil"), ("SSH_USER", "bad user"), ("SSH_PORT", "0"),
    ("DB_PORT", "65536"), ("DB_READONLY_HOST", "192.0.2.10"),
    ("TUNNEL_REMOTE_HOST", ""), ("TUNNEL_REMOTE_PORT", "not-a-port")])
def test_invalid_config(env, key, value):
    env["MANIFEST_BGD_" + key] = value
    with pytest.raises(ValueError):
        manifest_tunnels._config(env, "BGD")


def test_manifest_tunnel_accepts_explicit_key_without_password(env, tmp_path):
    key = tmp_path / "ssh-key"
    key.write_text("test")
    env.pop("MANIFEST_BGD_SSH_PASSWORD")
    env["MANIFEST_BGD_SSH_KEY"] = str(key)
    config = manifest_tunnels._config(env, "BGD")
    assert config["key"] == str(key)
    assert config["password"] is None


async def test_lifespan_configures_independent_sites(env, monkeypatch):
    env.update({k.replace("_BGD_", "_CAS_"): v for k, v in list(env.items())})
    env["MANIFEST_CAS_DB_PORT"] = "13308"
    monkeypatch.setattr(manifest_tunnels, "dotenv_values", lambda _: env)
    monkeypatch.delenv("MANIFEST_BGD_TUNNEL_ENABLED", raising=False)
    monkeypatch.delenv("MANIFEST_CAS_TUNNEL_ENABLED", raising=False)
    manager = manifest_tunnels.manager
    monkeypatch.setattr(manager, "start", lambda name: None)
    async with manifest_tunnels.manifest_tunnels():
        assert manager.status("bgd")["enabled"]
        assert manager.status("cas")["enabled"]
    assert not manager.status("bgd")["enabled"]
    assert not manager.status("cas")["enabled"]
