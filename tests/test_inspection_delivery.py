import asyncio, time
import pytest
from toposync.runtime.pipelines.inspection import InspectionChannel
from dataclasses import replace
from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    OperatorRegistry,
    PipelineGraphCompiler,
    PipelineRuntimeDependencies,
    SourceOperatorRuntime,
    SinkRuntime,
)
from toposync.runtime.pipelines.runtime import Packet, Lifecycle
from toposync.runtime.pipelines.shared_runtime import PipelineBundleRuntime
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.pipelines.inspection import PipelineInspectionStore


def test_result_reference_deliveries_preserve_producer_edges_without_exposing_dependencies():
    from test_pipeline_result_references import result_fixture
    from toposync.runtime.pipelines.demands import DemandRequest, ResourceDemandRegistry
    from toposync.runtime.pipelines.execution import PassThroughRuntime

    async def scenario():
        registry, producer, manual, automatic = result_fixture(lambda *_: PassThroughRuntime())
        compiler = PipelineGraphCompiler(registry)
        demands = ResourceDemandRegistry(lambda *_: True, compiler=compiler)
        demands.bind([producer])
        _, output, revision = demands.outputs["people.primary"]
        for pipeline in (manual, automatic):
            demand = demands.open("owner", DemandRequest(consumer_id=pipeline.name,
                purpose="manual" if pipeline is manual else "automatic", result_key=output.key,
                contract=output.contract, production_revision=revision, payload_keys=["subject"]))
            pipeline.graph["nodes"][0]["config"]["demand_id"] = demand.id
        store = PipelineInspectionStore()
        tokens = {pipeline.name: store.open(pipeline.name, "revision",
            {node["id"]: node["operator"] for node in pipeline.graph["nodes"]})
            for pipeline in (producer, manual, automatic)}
        bundle = PipelineBundleRuntime(compiler.compile_many([manual, automatic],
            catalog=[producer, manual, automatic]), registry, PipelineRuntimeDependencies(
                demand_registry=demands, inspection_store=store,
                inspection_revisions={name: "revision" for name in tokens}))
        contexts = bundle._runtime._context_by_node
        source = next(context for context in contexts.values()
            if ("producer", "source") in context.inspection_occurrences)
        tracker = next(context for context in contexts.values()
            if ("producer", "tracker") in context.inspection_occurrences)
        packet = Packet.create(stream_id="synthetic", payload={"subject": {"id": "synthetic"}})
        try:
            assert await source.emit(packet) == 1
            assert await tracker.read() is packet
            assert await tracker.emit(packet) == 2
            for name in ("manual", "automatic"):
                reference = contexts[bundle.plan.occurrence_to_merged_node_id[name, "reference"]]
                assert (await reference.read()).payload["subject"] == {"id": "synthetic"}
                snapshot = store.snapshot(tokens[name])
                assert [node.node_id for node in snapshot.nodes] == ["reference"]
                assert snapshot.nodes[0].received_count == 1
                assert snapshot.deliveries == []  # Imported dependency IDs are outside this session.
            snapshot = store.snapshot(tokens["producer"])
            assert {node.node_id: node.produced_count for node in snapshot.nodes} == {"source": 1, "tracker": 1}
            assert len(snapshot.deliveries) == 1
            delivery = snapshot.deliveries[0]
            assert (delivery.source_node_id, delivery.target_node_id) == ("source", "tracker")
            assert (delivery.accepted_count, delivery.received_count) == (1, 1)
        finally:
            for token in tokens.values():
                store.close(token)
            for identifier in list(demands.demands):
                demands.release(identifier)
            await bundle.stop()

    asyncio.run(asyncio.wait_for(scenario(), 3))


def test_shared_source_separates_production_queue_acceptance_receipt_and_reconciliation():
    async def scenario():
        registry = OperatorRegistry()
        registry.register_operator(
            operator_id="test.passive_source",
            inputs=[],
            outputs=[{"name": "out"}],
            runtime_factory=lambda c, d: SourceOperatorRuntime(),
        )
        registry.register_operator(
            operator_id="test.isolated_sink",
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            share_strategy="never",
            runtime_factory=lambda c, d: SinkRuntime(),
        )
        pipelines = []
        for name in ("manual", "generated"):
            graph = build_pipeline_graph_v2(
                graph_uid=name,
                nodes=[
                    {"id": "source", "operator": "test.passive_source"},
                    {"id": "sink", "operator": "test.isolated_sink"},
                ],
                edges=[
                    {
                        "from": {"node": "source", "port": "out"},
                        "to": {"node": "sink", "port": "in"},
                        "maxsize": 1,
                        "drop_policy": "block",
                    }
                ],
            )
            pipelines.append(Pipeline(name=name, graph=graph))
        store = PipelineInspectionStore()
        tokens = {
            p.name: store.open(
                p.name, "revision", {"source": "test.passive_source", "sink": "test.isolated_sink"}
            )
            for p in pipelines
        }
        bundle = PipelineBundleRuntime(
            PipelineGraphCompiler(registry).compile_many(pipelines),
            registry,
            PipelineRuntimeDependencies(
                inspection_store=store, inspection_revisions={p.name: "revision" for p in pipelines}
            ),
        )
        physical = bundle.plan.occurrence_to_merged_node_id
        assert physical["manual", "source"] == physical["generated", "source"]
        source = bundle._runtime._context_by_node[physical["manual", "source"]]
        manual = bundle._runtime._context_by_node[physical["manual", "sink"]]
        generated = bundle._runtime._context_by_node[physical["generated", "sink"]]

        def packet(sequence):
            return Packet.create(
                stream_id="synthetic",
                payload={
                    "source": {"device_id": "synthetic", "source_id": "main"},
                    "capture_evidence": {
                        "capture_instance": "shared",
                        "generation": 1,
                        "sequence": sequence,
                        "published_at": time.time(),
                    },
                },
            )

        try:
            first, second = packet(1), packet(2)
            assert await source.emit(first, timeout_s=0.001) == 2
            assert (await manual.read()).packet_id == first.packet_id
            # Keep generated queue occupied: next emission can reach only manual.
            accepted = await source.emit(second, timeout_s=0.001)
            assert accepted == 1
            assert (await manual.read()).packet_id == second.packet_id
            delayed = await generated.read()
            assert delayed.packet_id == first.packet_id
            snapshots = {
                name: store.snapshot(token).model_dump(mode="json")
                for name, token in tokens.items()
            }
            nodes = {
                name: {node["node_id"]: node for node in snapshot["nodes"]}
                for name, snapshot in snapshots.items()
            }
            assert (
                nodes["manual"]["source"]["produced_count"]
                == nodes["generated"]["source"]["produced_count"]
                == 2
            )
            assert nodes["manual"]["sink"]["frame"]["sequence"] == 2
            assert nodes["generated"]["sink"]["frame"]["sequence"] == 1
            manual_delivery = snapshots["manual"]["deliveries"][0]
            generated_delivery = snapshots["generated"]["deliveries"][0]
            assert (
                manual_delivery["accepted_count"],
                manual_delivery["received_count"],
                manual_delivery["rejected_count"],
            ) == (2, 2, 0)
            assert (
                generated_delivery["accepted_count"],
                generated_delivery["received_count"],
                generated_delivery["rejected_count"],
            ) == (1, 1, 1)
            assert (
                manual_delivery["producer_epoch"]
                == generated_delivery["producer_epoch"]
                == source.inspection_epoch
            )
            assert manual_delivery["physical_edge_id"] != generated_delivery["physical_edge_id"]
            assert (
                generated_delivery["state"] == "received"
                and generated_delivery["frame"]["sequence"] == 1
            )
            # Emitting is physical production even when every queue refuses a packet.
            assert await source.emit(packet(3), timeout_s=0.001) == 2
            assert await source.emit(packet(4), timeout_s=0.001) == 0
            assert all(
                store.snapshot(token).nodes[0].produced_count == 4 for token in tokens.values()
            )
            # Reconciliation replaces logical channels but retains the shared producer epoch.
            old_sequence = store.snapshot(tokens["generated"]).sequence
            third = pipelines[1].model_copy(update={"name": "third"})
            third_token = store.open(
                "third", "revision", {"source": "test.passive_source", "sink": "test.isolated_sink"}
            )
            tokens["third"] = third_token
            dependencies = PipelineRuntimeDependencies(
                inspection_store=store,
                inspection_revisions={"manual": "revision", "third": "revision"},
            )
            epoch = source.inspection_epoch
            await bundle.reconcile(
                PipelineGraphCompiler(registry).compile_many([pipelines[0], third]), dependencies
            )
            source = bundle._runtime._context_by_node[
                bundle.plan.occurrence_to_merged_node_id["manual", "source"]
            ]
            assert source.inspection_epoch == epoch
            retired = store.snapshot(tokens["generated"])
            assert next(node for node in retired.nodes if node.node_id == "sink").state == "closed"
            assert retired.sequence > old_sequence
            old_sequence = retired.sequence
            await source.emit(packet(5), timeout_s=0.001)
            assert store.snapshot(tokens["generated"]).sequence == old_sequence
            assert store.snapshot(third_token).deliveries[0].producer_epoch == epoch

        finally:
            for token in tokens.values():
                store.close(token)
            await bundle.stop()

    asyncio.run(scenario())


def test_delivery_scope_bounds_expiry_and_revocation():
    now = [100.0]
    store = PipelineInspectionStore(clock=lambda: now[0])
    token = store.open("manual", "revision", {"source": "source", "sink": "sink"})
    session = store.sessions[token]
    value = Packet.create(
        stream_id="synthetic",
        payload={
            "capture_evidence": {
                "capture_instance": "synthetic",
                "generation": 1,
                "sequence": 1,
                "published_at": 100.0,
            },
            "source": {"source_id": "main"},
            "occurrence_reference": {"occurrence_id": "private-occurrence"},
        },
    )

    def record(**changes):
        args = dict(
            pipeline="manual",
            revision="revision",
            channel=InspectionChannel("physical", "epoch", ()),
            source="source",
            source_port="out",
            target="sink",
            target_port="in",
            state="accepted",
            packet=value,
        )
        args.update(changes)
        store.observe_delivery(**args)

    record(pipeline="other")
    record(revision="changed")
    record(target="unauthorized")
    assert not store.snapshot(token).deliveries
    record()
    record(state="received")
    assert len(store.snapshot(token).deliveries) == 1
    assert session.state_bytes == sum(session.node_sizes.values()) + sum(
        session.delivery_sizes.values()
    )
    now[0] = 101.01
    delivery = store.snapshot(token).deliveries[0]
    assert delivery.state == "expired" and delivery.frame is None
    assert delivery.accepted_count == delivery.received_count == 1
    # An old sample counts as a real operation but cannot regain a current reference.
    record(state="timeout")
    assert store.snapshot(token).deliveries[0].state == "expired"
    assert store.snapshot(token).deliveries[0].rejected_count == 1
    store.maximum_items = 2
    record(source_port="second")
    record(source_port="third")
    assert len(store.snapshot(token).deliveries) == 2 and session.dropped == 1
    store.invalidate_occurrence("private-occurrence")
    assert token not in store.sessions and not session.deliveries and not session.delivery_sizes


def test_delivery_metadata_consumes_the_same_byte_budget_and_wakes_subscription():
    async def scenario():
        store = PipelineInspectionStore()
        token = store.open("manual", "revision", {"source": "source", "sink": "sink"})
        session = store.sessions[token]
        waiting = asyncio.create_task(store.wait_for_change(token, session.sequence))
        await asyncio.sleep(0)
        source = Packet.create(stream_id="synthetic", payload={})
        args = dict(
            pipeline="manual",
            revision="revision",
            channel=InspectionChannel("physical", "epoch", ()),
            source="source",
            source_port="out",
            target="sink",
            target_port="in",
            state="accepted",
            packet=source,
        )
        store.observe_delivery(**args)
        await asyncio.wait_for(waiting, 0.1)
        assert store.snapshot(token).sequence > 0
        store.maximum_bytes = session.state_bytes
        store.observe_delivery(**{**args, "source_port": "other"})
        assert token not in store.sessions

    asyncio.run(scenario())


def test_pending_response_expires_delivery_references_and_revocation_invalidates_it():
    from types import SimpleNamespace
    from fastapi import HTTPException
    from toposync.runtime.pipelines.inspection_api import finalize_inspection

    now = [100.0]
    store = PipelineInspectionStore(clock=lambda: now[0])
    token = store.open("manual", "revision", {"source": "source", "sink": "sink"})
    value = Packet.create(
        stream_id="synthetic",
        payload={
            "capture_evidence": {
                "capture_instance": "synthetic",
                "generation": 1,
                "sequence": 1,
                "published_at": 100.0,
            }
        },
    )
    store.observe_delivery(
        pipeline="manual",
        revision="revision",
        channel=InspectionChannel("physical", "epoch", ()),
        source="source",
        source_port="out",
        target="sink",
        target_port="in",
        state="accepted",
        packet=value,
    )
    pending = store.snapshot(token)
    assert pending.deliveries[0].frame is not None
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(pipeline_inspection=store)),
        state=SimpleNamespace(inspection_private_checks=[]),
    )
    now[0] = 101.01
    result = finalize_inspection(request, pending)
    assert result.deliveries[0].frame is None and result.deliveries[0].state == "expired"
    store.close(token)
    with pytest.raises(HTTPException) as caught:
        finalize_inspection(request, pending)
    assert caught.value.status_code == 410
