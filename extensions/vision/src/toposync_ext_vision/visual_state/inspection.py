"""Technical evidence only; no pixels, class descriptions or embedding vectors."""


def decision_visuals(state):
    properties = {
        "runtime_epoch": state.runtime_epoch, "state_version": state.state_version,
        "preparation_revision": state.preparation_revision, "reference_revision": state.reference_revision,
        "model_revision": state.model_revision, "reason": state.reason, "valid_for_ms": state.valid_for_ms,
    }
    for key, value in (("class_id", state.class_id),
                       ("candidate_class_id", state.candidate.class_id if state.candidate else None),
                       ("last_confirmed_class_id", state.last_confirmed.class_id if state.last_confirmed else None)):
        if value is not None:
            properties[key] = value
    result = [{"layer": "visual_state", "kind": "points", "points": [], "subject_id": state.sensor_id,
               "state": state.status, "properties": properties}]
    analysis = state.last_analysis
    if analysis is not None:
        properties = {"capture_instance": analysis.sample.capture_instance, "generation": analysis.sample.generation,
                      "sequence": analysis.sample.sequence, "observed_at": analysis.observed_at,
                      "age_ms": state.analysis_age_ms, "duration_ms": analysis.duration_ms,
                      "freshness_basis": analysis.freshness_basis, "region_quality": analysis.region_quality,
                      "reason": analysis.reason, "method": analysis.method}
        if analysis.margin is not None:
            properties["margin"] = analysis.margin
        if analysis.declared_confidence is not None:
            properties["declared_confidence"] = analysis.declared_confidence
        result.append({"layer": "visual_analysis", "kind": "points", "points": [],
                       "state": analysis.acceptance, "properties": properties,
                       "score": analysis.similarity, "score_kind": "cosine_similarity",
                       "related_subject_ids": list(analysis.reference_ids[:32])})
    return result
