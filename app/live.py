"""Isolated in-process live resources for each authenticated warehouse."""

from dataclasses import dataclass

from .events import EventBus
from .pipeline import Pipeline
from .sources import SourceManager
from .warehouse import current_warehouse


@dataclass
class LiveResources:
    source_mgr: SourceManager
    bus: EventBus
    pipeline: Pipeline


def live_resources(app, warehouse: str | None = None) -> LiveResources:
    access = warehouse or current_warehouse()
    if access not in ("bgd", "cas"):
        raise ValueError("invalid warehouse")
    if not hasattr(app.state, "warehouse_runtimes"):
        app.state.warehouse_runtimes = {}
    runtimes = app.state.warehouse_runtimes
    if access not in runtimes:
        if access == "bgd":
            runtimes[access] = LiveResources(app.state.source_mgr, app.state.bus, app.state.pipeline)
        else:
            source_mgr = SourceManager(app.state.settings)
            bus = EventBus()
            runtimes[access] = LiveResources(source_mgr, bus, Pipeline(app.state.settings, app.state.dedup, bus, source_mgr))
    return runtimes[access]
