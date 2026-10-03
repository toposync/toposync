from copy import deepcopy

import pytest

from toposync_ext_cameras.pipeline_observation import pipeline_execution_observation


def snapshot():
    return {
        "running": True,
        "pipelines": [{"name": "entrance"}],
        "graph_info": {"graphs": [{
            "graph_id": "local_bundle", "running": True,
            "node_occurrences": {
                "source": [{"pipeline_name": "entrance"}, {"pipeline_name": "other"}],
                "detect": [{"pipeline_name": "entrance"}],
                "foreign": [{"pipeline_name": "other"}],
            },
            "nodes": {
                "source": {"task_state": "running", "last_received_at": None,
                           "last_completed_at": 200},
                "detect": {"task_state": "running", "last_received_at": 90,
                           "last_completed_at": 91},
                "foreign": {"task_state": "failed", "last_completed_at": 300,
                            "last_received_at": 299, "unavailable_reason": "Private detail"},
            },
        }]},
    }


def observe(status, server="local"):
    return pipeline_execution_observation(status, pipeline_name="entrance", processing_server_id=server)


def test_shared_source_and_other_pipeline_do_not_certify_processing_or_leak_errors():
    result = observe(snapshot())
    assert result == {"state": "observed", "last_received_at": 90, "last_completed_at": 91}
    assert observe(snapshot(), "remote") is None


def test_query_time_does_not_refresh_old_processing():
    status = snapshot()
    status["graph_info"]["graphs"][0]["generated_at"] = 10000
    assert observe(status)["last_completed_at"] == 91
    status["graph_info"]["graphs"][0]["generated_at"] = 20000
    assert observe(status)["last_completed_at"] == 91


@pytest.mark.parametrize("change,expected", [
    ({"last_completed_at": None}, "waiting"),
    ({"unavailable_reason": "unsupported"}, "unavailable"),
    ({"last_error_at": 92}, "unavailable"),
    ({"last_error_at": 80, "error_count": 1}, "observed"),
    ({"task_state": "failed"}, "interrupted"),
    ({"task_state": "canceled"}, "interrupted"),
    ({"last_completed_at": float("nan")}, "waiting"),
])
def test_processing_state_preserves_availability_and_recovery(change, expected):
    status = snapshot()
    status["graph_info"]["graphs"][0]["nodes"]["detect"].update(change)
    assert observe(status)["state"] == expected


def test_unavailable_legacy_or_missing_graph_does_not_certify_running():
    assert observe(None) is None
    assert observe({}) is None
    status = snapshot()
    status["graph_info"]["graphs"] = []
    assert observe(status) is None
    status["pipelines"] = []
    assert observe(status)["state"] == "not_running"
    status = snapshot()
    status["running"] = False
    assert observe(status)["state"] == "not_running"
    status = snapshot()
    for node in status["graph_info"]["graphs"][0]["nodes"].values():
        node.pop("last_completed_at", None)
    assert observe(status) is None


def test_direct_graph_has_same_observation_as_bundle_projection():
    status = snapshot()
    expected = observe(status)
    direct = deepcopy(status["graph_info"]["graphs"][0])
    direct["graph_id"] = "entrance"
    direct["nodes"].pop("foreign")
    direct.pop("node_occurrences")
    status["graph_info"]["graphs"] = [direct]
    assert observe(status) == expected
