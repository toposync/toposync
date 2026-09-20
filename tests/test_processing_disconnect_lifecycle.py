import asyncio

from toposync.runtime.config_store import ConfigStore, Pipeline, ProcessingServer, UserDataPaths
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines import Lifecycle, OperatorRegistry, Packet, PipelineGraphCompiler, register_builtin_operators
from toposync.runtime.pipelines.distributed import orchestrator as module
from toposync.runtime.pipelines.operators_distributed import _serialize_packet


def test_processing_disconnect_closes_only_affected_origin_and_resumes_with_empty_state(tmp_path, monkeypatch):
    async def scenario():
        disconnect = asyncio.Event()
        reconnect = asyncio.Event()
        opened = asyncio.Event()
        closed = asyncio.Event()
        resumed = asyncio.Event()
        delivered = []

        class Transport:
            calls = 0

            def __init__(self, **_arguments):
                pass

            async def push_config(self, _payload):
                if disconnect.is_set():
                    await reconnect.wait()

            async def stream_events(self, **_arguments):
                self.calls += 1
                if self.calls == 1:
                    packet = Packet.create(stream_id="actor-one", lifecycle=Lifecycle.OPEN,
                        payload={"subject": {"id": "actor-one", "category": "person"}, "frame_ts": 1.0})
                    yield {"event_id": 1, "pipeline_name": "affected", "target_node_id": "notify",
                           "target_port": "in", "packet": _serialize_packet(packet)}
                    await disconnect.wait()
                    raise ConnectionError("controlled processing disconnect")
                # A transient transport failure does not restart the remote
                # tracker. Its actor may continue, but the closed observation
                # must remain closed and a new observation starts at this sample.
                packet = Packet.create(stream_id="actor-one", lifecycle=Lifecycle.UPDATE,
                    payload={"subject": {"id": "actor-one", "category": "person"}, "frame_ts": 10.0})
                yield {"event_id": 2, "pipeline_name": "affected", "target_node_id": "notify",
                       "target_port": "in", "packet": _serialize_packet(packet)}
                await asyncio.Event().wait()

            async def ack(self, _identifier):
                pass

            async def close(self):
                pass

        monkeypatch.setattr(module, "HttpProcessingTransport", Transport)
        paths = UserDataPaths(data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files")
        notifications = NotificationsRuntime(data_dir=tmp_path / "notifications")
        original = notifications.upsert

        async def observe(**arguments):
            result = await original(**arguments)
            payload = arguments.get("payload") or {}
            delivered.append((result["id"], payload))
            if payload.get("lifecycle") == "open":
                opened.set()
            if payload.get("lifecycle") == "close":
                closed.set()
            if payload.get("lifecycle") == "update":
                resumed.set()
            return result

        notifications.upsert = observe
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        owner = module.PipelinesOrchestrator(config_store=ConfigStore(paths=paths),
            operator_registry=registry, compiler=PipelineGraphCompiler(registry),
            notifications=notifications, files_dir=paths.files_dir)
        graph = {"schema_version": 2, "uid": "disconnect", "nodes": [
            {"uid": "source", "id": "source", "operator": "core.demo_frame_sequence_source", "config": {}},
            {"uid": "notify", "id": "notify", "operator": "core.notify", "config": {}},
        ], "edges": [{"uid": "observe", "from": {"node": "source", "port": "out"},
                       "to": {"node": "notify", "port": "in"}, "traffic": {"modality": "video"}}]}
        affected = Pipeline(name="affected", processing_server_id="edge", graph=graph)
        other = Pipeline(name="other", processing_server_id="unrelated", graph=graph)
        server = ProcessingServer(id="edge", name="Controlled edge", kind="http", url="http://127.0.0.1:1")
        try:
            await owner._start_origin_pipeline_for_remote(affected)
            await owner._start_origin_pipeline_for_remote(other)
            first = owner._pipelines["affected"].runtime
            unrelated = owner._pipelines["other"].runtime
            await owner._start_remote_server(server, [affected], settings_payload={"core": {}, "extensions": {}})
            await asyncio.wait_for(opened.wait(), 1)
            disconnect.set()
            await asyncio.wait_for(closed.wait(), 1)
            assert delivered[0][0] == delivered[-1][0]
            assert first._runtime_by_node["notify"]._state == {}
            assert owner._pipelines["other"].runtime is unrelated
            reconnect.set()
            await asyncio.wait_for(resumed.wait(), 2)
            assert owner._pipelines["affected"].runtime is not first
            assert delivered[-1][0] != delivered[0][0]
            previous = await notifications.get(delivered[0][0])
            assert previous["payload"]["status"] == "closed"
            assert delivered[-1][1]["event"]["duration_seconds"] == 0
        finally:
            await owner.stop()
            assert not owner._pipelines and not owner._inboxes and not owner._servers
            notifications.store._conn.close()

    asyncio.run(scenario())
