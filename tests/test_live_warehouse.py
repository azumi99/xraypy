from types import SimpleNamespace

from app import live
from app.warehouse import warehouse_context


def test_live_resources_are_isolated_by_warehouse(monkeypatch):
    state = SimpleNamespace(source_mgr=object(), bus=object(), pipeline=object(), settings=object(), dedup=object())
    app = SimpleNamespace(state=state)
    monkeypatch.setattr(live, "SourceManager", lambda settings: object())
    monkeypatch.setattr(live, "Pipeline", lambda settings, dedup, bus, sources: object())
    bgd = live.live_resources(app, "bgd")
    token = warehouse_context.set("cas")
    try:
        cas = live.live_resources(app)
        assert cas is live.live_resources(app, "cas")
        assert cas.source_mgr is not bgd.source_mgr
        assert cas.bus is not bgd.bus
        assert cas.pipeline is not bgd.pipeline
    finally:
        warehouse_context.reset(token)
