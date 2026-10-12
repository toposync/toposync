"""Loopback fixture with synthetic compositions and no configured hardware."""
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import Request
import uvicorn


REFERENCE = {"version": 1, "latitude": -27.596, "longitude": -48.549, "rotationDegrees": 37,
             "scale": 1.2, "axes": "x-east-z-south", "units": "metres"}


def composition(identifier, name, longitude):
    return {"id": identifier, "name": name, "meta": {"georeference": {**REFERENCE, "longitude": longitude}}, "elements": [
        {"id": "floor", "name": "Synthetic floor", "type": "com.toposync.structural.area", "position": {"x": 5, "z": 5},
         "props": {"vertices": [{"x": 0, "z": 0}, {"x": 12, "z": 0}, {"x": 12, "z": 12}, {"x": 0, "z": 12}], "fill": "#67887b", "opacity": 1}},
        {"id": "same-switch", "name": f"{name} switch", "type": "com.toposync.home_assistant.item", "position": {"x": 2, "z": 2},
         "props": {"server_id": "synthetic", "items": [{"kind": "entity", "id": f"switch.{identifier}"}], "primary_entity_id": f"switch.{identifier}", "primary_state": "off", "icon": "lightbulb"}},
    ]}


def make_app(directory):
    sentinel = directory / ".geographic-browser-fixture"
    if directory.exists() and any(directory.iterdir()) and not sentinel.is_file():
        raise RuntimeError("Use an empty dedicated test data directory")
    directory.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("Synthetic geographic test data\n")
    first, second = composition("geographic-first", "First", -48.549), composition("geographic-second", "Second", -48.5487)
    config = {"schema_version": 1, "active_composition_id": first["id"], "compositions": [first, second, {"id": "unpositioned", "name": "Unpositioned", "elements": []}], "settings": {"core": {}, "extensions": {}}, "pipelines": []}
    (directory / "config.json").write_text(json.dumps(config))
    os.environ["TOPOSYNC_DATA_DIR"] = str(directory.resolve())
    os.environ["TOPOSYNC_AUTH_MODE"] = "bypass"
    from toposync.app import create_app
    from toposync.runtime.config_store import Composition
    import toposync.extensions.manager as extensions
    original_entries = extensions._iter_entry_points
    extensions._iter_entry_points = lambda group: [entry for entry in original_entries(group) if entry.name in {"structural", "home_assistant"}]
    app = create_app()
    original_lifespan = app.router.lifespan_context

    async def occurrence(points=None):
        return await app.state.notifications.upsert(type="pipelines.tracking", title="Synthetic detection", dedupe_key="geographic-trail",
            payload={"composition_id": first["id"], "trail": points or [{"x": 3, "z": 3}, {"x": 6, "z": 3}, {"x": 6, "z": 6}], "priority": "high", "lifecycle": "close"})

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            await occurrence()
            yield
    app.router.lifespan_context = lifespan

    @app.get("/api/__geographic_fixture")
    async def information():
        return {"synthetic": True, "reference": REFERENCE}

    @app.post("/api/__geographic_fixture/reset")
    async def reset():
        for value in (second, first):
            await app.state.config_store.set_active_composition(Composition.model_validate(value))
        return await occurrence()

    @app.post("/api/__geographic_fixture/notification")
    async def update(request: Request):
        return await occurrence((await request.json()).get("trail"))

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8137)
    args = parser.parse_args()
    uvicorn.run(make_app(args.data_dir), host="127.0.0.1", port=args.port, log_level="warning")
