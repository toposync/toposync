import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n } from "@toposync/plugin-api";

import { createUniqueId } from "../parsing";
import type { CameraDetection } from "../types";
import { SubModal } from "../ui/sub_modal";

import { DetectionConditionEditor, describeDetectionCondition } from "./detection_condition_editor";

export function CameraDetectionsModal({
  open,
  onClose,
  i18n,
  cameraLabel,
  initialDetections,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  i18n: HostI18n;
  cameraLabel: string;
  initialDetections: CameraDetection[];
  onSave: (detections: CameraDetection[]) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();

  const [detections, setDetections] = useState<CameraDetection[]>([]);
  const [selectedDetectionId, setSelectedDetectionId] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const normalized = (initialDetections ?? []).map((detection) => ({
      id: detection.id || createUniqueId(),
      trigger: detection.trigger ?? { kind: "motion" },
      filters: Array.isArray(detection.filters) ? detection.filters : [],
    }));
    setDetections(normalized);
    setSelectedDetectionId(normalized[0]?.id ?? null);
  }, [open, initialDetections]);

  const selectedDetection = useMemo(
    () => (selectedDetectionId ? detections.find((detection) => detection.id === selectedDetectionId) ?? null : null),
    [detections, selectedDetectionId],
  );

  function addDetection() {
    const id = createUniqueId();
    setDetections((previous) => [{ id, trigger: { kind: "motion" }, filters: [] }, ...previous]);
    setSelectedDetectionId(id);
  }

  function updateDetection(id: string, patch: Partial<CameraDetection>) {
    setDetections((previous) => previous.map((detection) => (detection.id === id ? { ...detection, ...patch } : detection)));
  }

  function deleteDetection(id: string) {
    setDetections((previous) => previous.filter((detection) => detection.id !== id));
    setSelectedDetectionId((previous) => (previous === id ? null : previous));
  }

  return (
    <SubModal
      open={open}
      onClose={onClose}
      title={cameraLabel ? `${t("ext.cameras.detections.title")}: ${cameraLabel}` : t("ext.cameras.detections.title")}
      panelStyle={{ width: "min(1100px, calc(100vw - 28px))" }}
      bodyStyle={{
        padding: 0,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 12, flex: 1, minHeight: 0 }}>
        <div className="card">
          <div className="cardBody">{t("ext.cameras.detections.help")}</div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 12, flex: 1, minHeight: 0 }}>
          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <div className="label" style={{ margin: 0 }}>
                {t("ext.cameras.detections.list")}
              </div>
              <button
                className="iconButton iconButtonPrimary"
                type="button"
                onClick={addDetection}
                aria-label={t("core.actions.add")}
              >
                <i className="fa-solid fa-plus" aria-hidden="true" />
              </button>
            </div>

            <div className="sectionDivider" />

            <div style={{ overflow: "auto", minHeight: 0, paddingRight: 2 }}>
              {detections.length === 0 ? (
                <div className="card">
                  <div className="cardBody">{t("ext.cameras.detections.empty")}</div>
                </div>
              ) : (
                <div className="choiceList">
                  {detections.map((detection, index) => {
                    const isSelected = selectedDetectionId === detection.id;
                    const summary = describeDetectionCondition(detection.trigger, t);
                    const filtersCount = detection.filters.length;
                    return (
                      <button
                        key={detection.id}
                        type="button"
                        className={["choiceItem", isSelected ? "isSelected" : ""].join(" ")}
                        onClick={() => setSelectedDetectionId(detection.id)}
                      >
                        <div style={{ display: "flex", flexDirection: "column", gap: 4, width: "100%" }}>
                          <div className="row" style={{ justifyContent: "space-between", gap: 10 }}>
                            <span style={{ fontWeight: 700 }}>{t("ext.cameras.detections.item", { n: index + 1 })}</span>
                            {filtersCount ? (
                              <span
                                style={{
                                  minWidth: 24,
                                  height: 20,
                                  padding: "0 8px",
                                  borderRadius: 999,
                                  display: "inline-flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  fontSize: 12,
                                  fontWeight: 700,
                                  border: "1px solid rgba(255,255,255,0.14)",
                                  background: "rgba(255,255,255,0.06)",
                                  color: "rgba(230,232,242,0.92)",
                                }}
                              >
                                {filtersCount}
                              </span>
                            ) : null}
                          </div>
                          <div className="cardMeta" style={{ margin: 0 }}>
                            {summary}
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            {selectedDetection ? (
              <div className="card" style={{ overflow: "hidden", display: "flex", flexDirection: "column", minHeight: 0 }}>
                <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
                  <div className="label" style={{ margin: 0 }}>
                    {t("ext.cameras.detections.details")}
                  </div>
                  <button
                    className="iconButton iconButtonDanger"
                    type="button"
                    onClick={() => deleteDetection(selectedDetection.id)}
                    aria-label={t("core.actions.delete")}
                  >
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                  </button>
                </div>

                <div className="sectionDivider" />

                <div style={{ display: "flex", flexDirection: "column", gap: 12, minHeight: 0 }}>
                  <div className="field">
                    <label className="label">{t("ext.cameras.detections.trigger")}</label>
                    <DetectionConditionEditor
                      value={selectedDetection.trigger}
                      i18n={i18n}
                      onChange={(next) => updateDetection(selectedDetection.id, { trigger: next })}
                    />
                  </div>

                  <div className="field">
                    <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
                      <label className="label" style={{ margin: 0 }}>
                        {t("ext.cameras.detections.filters")}
                      </label>
                      <button
                        className="iconButton"
                        type="button"
                        onClick={() => {
                          updateDetection(selectedDetection.id, { filters: [...selectedDetection.filters, { kind: "motion" }] });
                        }}
                        aria-label={t("ext.cameras.detections.add_filter")}
                      >
                        <i className="fa-solid fa-plus" aria-hidden="true" />
                      </button>
                    </div>

                    {selectedDetection.filters.length === 0 ? (
                      <div className="card">
                        <div className="cardBody">{t("ext.cameras.detections.filters_empty")}</div>
                      </div>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                        {selectedDetection.filters.map((filter, filterIndex) => (
                          <div className="rowWrap" key={filterIndex} style={{ gap: 8, alignItems: "center" }}>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <DetectionConditionEditor
                                value={filter}
                                i18n={i18n}
                                onChange={(next) => {
                                  const nextFilters = selectedDetection.filters.map((previous, index) =>
                                    index === filterIndex ? next : previous,
                                  );
                                  updateDetection(selectedDetection.id, { filters: nextFilters });
                                }}
                              />
                            </div>
                            <button
                              className="iconButton iconButtonDanger"
                              type="button"
                              onClick={() => {
                                const nextFilters = selectedDetection.filters.filter((_, index) => index !== filterIndex);
                                updateDetection(selectedDetection.id, { filters: nextFilters });
                              }}
                              aria-label={t("core.actions.delete")}
                            >
                              <i className="fa-solid fa-trash" aria-hidden="true" />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <div className="card">
                <div className="cardBody">{t("ext.cameras.detections.select_prompt")}</div>
              </div>
            )}
          </div>
        </div>

        <div className="rowWrap" style={{ justifyContent: "space-between" }}>
          <button className="chipButton" type="button" onClick={onClose}>
            {t("core.actions.cancel")}
          </button>
          <button
            className="primaryButton"
            type="button"
            onClick={() => {
              onSave(detections);
              onClose();
            }}
          >
            {t("core.actions.save")}
          </button>
        </div>
      </div>
    </SubModal>
  );
}

