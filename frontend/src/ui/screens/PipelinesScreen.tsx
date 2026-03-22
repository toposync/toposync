import Editor from "@monaco-editor/react";
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { i18n } from "../../util/i18n";
import { replace, usePathname } from "../router";
import type {
  CamerasIndexResponse,
  PipelineTemplateApplyCamerasRequest,
  PipelineTemplateApplyCamerasResponse,
  Pipeline,
  PipelineAlert,
  PipelineCompileOutput,
  PipelineCompilePythonOutput,
  PipelineOperatorDefinition,
  PipelineStats,
  ProcessingServer,
} from "../../util/api";
import {
  applyPipelineTemplateToCameras,
  compilePipeline,
  compilePipelinePython,
  createPipeline,
  deletePipeline,
  duplicatePipeline,
  getPipelineStats,
  listCamerasIndex,
  listPipelineOperators,
  listPipelines,
  listProcessingServers,
  putPipeline,
  resetPipelineStats,
} from "../../util/api";
import { InteractivePipelineEditor } from "./pipelines/InteractivePipelineEditor";
import { PipelineDuplicateModal } from "./pipelines/PipelineDuplicateModal";
import { PipelineTelemetryFieldModal } from "./pipelines/PipelineTelemetryFieldModal";
import { PipelineTelemetryOverviewCard } from "./pipelines/PipelineTelemetryOverviewCard";
import { PipelineTemplateApplyModal } from "./pipelines/PipelineTemplateApplyModal";
import type { EditorMode, InteractiveStep, TelemetryFieldInspectorRequest } from "./pipelines/types";
import {
  buildGraphFromInteractiveSteps,
  buildInteractiveStepsFromGraph,
  defaultPipeline,
  emptyGraph,
  isRecord,
  jsonPretty,
  safeJsonParse,
} from "./pipelines/utils";

type Props = {
  onClose: () => void;
  onOpenProcessingServers?: () => void;
};

const PIPELINES_BASE_PATH = "/settings/pipelines";

function pipelineNameFromPipelinesPath(pathname: string): string | null {
  const raw = String(pathname || "").trim();
  if (!raw || raw === "/" || raw === PIPELINES_BASE_PATH) return null;
  if (raw === PIPELINES_BASE_PATH + "/") return null;
  if (!raw.startsWith(PIPELINES_BASE_PATH + "/")) return null;
  const rest = raw.slice(PIPELINES_BASE_PATH.length + 1);
  const segment = rest.split("/")[0] ?? "";
  if (!segment) return null;
  try {
    return decodeURIComponent(segment).trim() || null;
  } catch {
    return segment.trim() || null;
  }
}

function buildPipelinesPath(pipelineName: string | null): string {
  if (!pipelineName) return PIPELINES_BASE_PATH;
  return `${PIPELINES_BASE_PATH}/${encodeURIComponent(String(pipelineName))}`;
}

export function PipelinesScreen({ onClose, onOpenProcessingServers }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const pathname = usePathname();
  const routePipelineName = pipelineNameFromPipelinesPath(pathname);
  const isAggregateHome = routePipelineName == null;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [servers, setServers] = useState<ProcessingServer[]>([]);
  const [compactLayout, setCompactLayout] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.matchMedia("(max-width: 960px)").matches;
    } catch {
      return false;
    }
  });
  const [sidebarOpen, setSidebarOpen] = useState(() => !compactLayout);
  const [templateApplyOpen, setTemplateApplyOpen] = useState(false);
  const [duplicateOpen, setDuplicateOpen] = useState(false);
  const [operators, setOperators] = useState<PipelineOperatorDefinition[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return pipelineNameFromPipelinesPath(window.location.pathname) ?? null;
  });
  const [camerasIndex, setCamerasIndex] = useState<CamerasIndexResponse>({ cameras: [] });

  const [createName, setCreateName] = useState("");

  const [draft, setDraft] = useState<Pipeline | null>(null);
  const [graphText, setGraphText] = useState<string>("");
  const [pythonText, setPythonText] = useState<string>("");
  const [mode, setMode] = useState<EditorMode>("interactive");
  const [compileOutput, setCompileOutput] = useState<(PipelineCompileOutput | PipelineCompilePythonOutput) | null>(null);

  const [recommendations, setRecommendations] = useState<PipelineAlert[]>([]);
  const [recommendationsError, setRecommendationsError] = useState<string | null>(null);
  const [recommendationsLoading, setRecommendationsLoading] = useState(false);

  const [interactiveSteps, setInteractiveSteps] = useState<InteractiveStep[]>([]);
  const [interactiveWarning, setInteractiveWarning] = useState<string | null>(null);

  const [pipelineStats, setPipelineStats] = useState<PipelineStats | null>(null);
  const [telemetryFieldInspector, setTelemetryFieldInspector] = useState<TelemetryFieldInspectorRequest | null>(null);
  const [telemetryResetting, setTelemetryResetting] = useState(false);
  const [telemetryResetNonce, setTelemetryResetNonce] = useState(0);

  const operatorsById = useMemo(() => {
    const entries = operators.map((operator) => [operator.id, operator] as const);
    return Object.fromEntries(entries);
  }, [operators]);

  const selected = useMemo(() => {
    if (!selectedName) return null;
    return pipelines.find((pipeline) => pipeline.name === selectedName) ?? null;
  }, [pipelines, selectedName]);

  const interactiveGraph = useMemo(
    () => buildGraphFromInteractiveSteps(interactiveSteps, operatorsById),
    [interactiveSteps, operatorsById],
  );

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [pipelineList, serverList, operatorList, cameras] = await Promise.all([
        listPipelines(),
        listProcessingServers(),
        listPipelineOperators(),
        listCamerasIndex().catch(() => ({ cameras: [] })),
      ]);
      setPipelines(pipelineList);
      setServers(serverList);
      setOperators(operatorList);
      setCamerasIndex(cameras);
      setSelectedName((prev) => {
        if (pipelineList.length === 0) return null;
        const fromPath = typeof window === "undefined" ? null : pipelineNameFromPipelinesPath(window.location.pathname);
        if (!fromPath) return null;
        if (pipelineList.some((pipeline) => pipeline.name === fromPath)) return fromPath;
        if (prev && pipelineList.some((pipeline) => pipeline.name === prev)) return prev;
        return null;
      });
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    const fromPath = pipelineNameFromPipelinesPath(pathname);
    setSelectedName((prev) => (prev === fromPath ? prev : fromPath));
  }, [pathname]);

  useEffect(() => {
    if (!selectedName) return;
    const fromPath = pipelineNameFromPipelinesPath(pathname);
    if (fromPath === selectedName) return;
    replace(buildPipelinesPath(selectedName));
  }, [pathname, selectedName]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    let mql: MediaQueryList | null = null;
    try {
      mql = window.matchMedia("(max-width: 960px)");
    } catch {
      return;
    }

    const apply = () => setCompactLayout(Boolean(mql?.matches));
    apply();
    mql.addEventListener("change", apply);
    return () => mql?.removeEventListener("change", apply);
  }, []);

  useEffect(() => {
    setSidebarOpen(!compactLayout);
  }, [compactLayout]);

  useEffect(() => {
    if (!compactLayout || !sidebarOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [compactLayout, sidebarOpen]);

  useEffect(() => {
    if (!selected) {
      setDraft(null);
      setGraphText("");
      setPythonText("");
      setMode("interactive");
      setCompileOutput(null);
      setInteractiveSteps([]);
      setInteractiveWarning(null);
      return;
    }

    setDraft(selected);
    setGraphText(jsonPretty(selected.graph ?? emptyGraph()));
    setPythonText(String(selected.python_source ?? ""));
    setMode((selected.editor_mode as EditorMode) ?? "interactive");
    setCompileOutput(null);
    setTelemetryFieldInspector(null);

    const loaded = buildInteractiveStepsFromGraph(selected.graph, operatorsById);
    setInteractiveSteps(loaded.steps);
    setInteractiveWarning(loaded.warning);
  }, [selected, operatorsById]);

  useEffect(() => {
    if (mode !== "interactive") return;
    if (!interactiveGraph.graph) return;
    setGraphText(jsonPretty(interactiveGraph.graph));
  }, [mode, interactiveGraph.graph]);

  const isPythonLocked = Boolean(draft && draft.editor_mode === "python");

  const switchMode = (nextMode: EditorMode) => {
    if (!draft) return;
    if (isPythonLocked && nextMode !== "python") return;

    if (nextMode === "interactive" && mode === "json") {
      const parsed = safeJsonParse(graphText);
      if (!parsed.ok) {
        setError(`Invalid graph JSON: ${parsed.error}`);
        return;
      }
      const loaded = buildInteractiveStepsFromGraph(parsed.data, operatorsById);
      setInteractiveSteps(loaded.steps);
      setInteractiveWarning(loaded.warning);
    }

    if (nextMode === "json" && mode === "interactive") {
      if (!interactiveGraph.graph) {
        setError(interactiveGraph.error || "Interactive graph is invalid.");
        return;
      }
      setGraphText(jsonPretty(interactiveGraph.graph));
    }

    setError(null);
    setMode(nextMode);
  };

  const resolveGraphFromActiveMode = (): { ok: true; graph: Record<string, unknown> } | { ok: false; message: string } => {
    if (!draft) return { ok: false, message: "No pipeline selected." };

    if (mode === "interactive") {
      if (!interactiveGraph.graph) {
        return { ok: false, message: interactiveGraph.error || "Interactive graph is invalid." };
      }
      return { ok: true, graph: interactiveGraph.graph };
    }

    if (mode === "json") {
      const parsed = safeJsonParse(graphText);
      if (!parsed.ok) return { ok: false, message: `Invalid graph JSON: ${parsed.error}` };
      if (!isRecord(parsed.data)) return { ok: false, message: "Graph JSON must be an object." };
      return { ok: true, graph: parsed.data };
    }

    // Python mode compiles to graph on save/compile. Keep the latest JSON around for reference.
    const parsed = safeJsonParse(graphText);
    if (parsed.ok && isRecord(parsed.data)) return { ok: true, graph: parsed.data };
    const graph = isRecord(draft.graph) ? draft.graph : emptyGraph();
    return { ok: true, graph };
  };

  useEffect(() => {
    if (!draft) {
      setRecommendations([]);
      setRecommendationsError(null);
      setRecommendationsLoading(false);
      return;
    }

    let cancelled = false;
    setRecommendationsLoading(true);
    setRecommendationsError(null);

    const handle = window.setTimeout(() => {
      const run = async () => {
        try {
          if (mode === "python") {
            if (!pythonText.trim()) {
              setRecommendations([]);
              setRecommendationsError(null);
              return;
            }
            const output = await compilePipelinePython({
              ...draft,
              editor_mode: "python",
              python_source: pythonText,
            });
            if (cancelled) return;
            setRecommendations(Array.isArray(output.alerts) ? output.alerts : []);
            setRecommendationsError(null);
            return;
          }

          const resolved = resolveGraphFromActiveMode();
          if (!resolved.ok) {
            if (cancelled) return;
            setRecommendations([]);
            setRecommendationsError(resolved.message);
            return;
          }
          const output = await compilePipeline({ ...draft, graph: resolved.graph });
          if (cancelled) return;
          setRecommendations(Array.isArray(output.alerts) ? output.alerts : []);
          setRecommendationsError(null);
        } catch (err: any) {
          if (cancelled) return;
          setRecommendations([]);
          setRecommendationsError(String(err?.message ?? err));
        } finally {
          if (cancelled) return;
          setRecommendationsLoading(false);
        }
      };

      void run();
    }, 450);

    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [draft, mode, interactiveGraph.graph, graphText, pythonText]);

  useEffect(() => {
    if (!draft) {
      setPipelineStats(null);
      return;
    }

    let cancelled = false;
    setPipelineStats(null);

    getPipelineStats(draft.name)
      .then((stats) => {
        if (cancelled) return;
        setPipelineStats(stats);
      })
      .catch(() => {
        if (cancelled) return;
        setPipelineStats(null);
      });

    return () => {
      cancelled = true;
    };
  }, [draft?.name]);

  const stepOutputsByNodeId = useMemo(() => {
    if (!pipelineStats || typeof pipelineStats !== "object") return null;
    const raw = (pipelineStats as any).node_outputs;
    if (!raw || typeof raw !== "object") return null;
    return raw as Record<string, number>;
  }, [pipelineStats]);

  const openTelemetryFieldInspector = useCallback((request: TelemetryFieldInspectorRequest) => {
    setTelemetryFieldInspector(request);
  }, []);

  const resetTelemetryAndStats = useCallback(async () => {
    const name = draft?.name ?? null;
    if (!name) return;
    setTelemetryResetting(true);
    setError(null);
    try {
      const stats = await resetPipelineStats(name);
      setPipelineStats(stats);
      setTelemetryResetNonce((prev) => prev + 1);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setTelemetryResetting(false);
    }
  }, [draft?.name]);

  const applyTelemetryFieldValue = useCallback(
    async (value: number) => {
      const target = telemetryFieldInspector;
      if (!target) return;
      const nextValue = Number.isFinite(value) ? value : 0;
      setInteractiveSteps((prev) =>
        prev.map((step) => {
          if (step.uid !== target.stepUid) return step;
          const parsed = safeJsonParse(step.configText || "{}");
          const base = parsed.ok && isRecord(parsed.data) ? { ...(parsed.data as Record<string, unknown>) } : {};
          base[target.configKey] = nextValue;
          return { ...step, configText: jsonPretty(base) };
        }),
      );
      setTelemetryFieldInspector((prev) => (prev ? { ...prev, value: nextValue } : prev));
    },
    [telemetryFieldInspector],
  );

  const handleCreate = async () => {
    const name = createName.trim();
    if (!name) return;
    setError(null);
    try {
      const created = await createPipeline(defaultPipeline(name, "final"));
      setPipelines((prev) => [...prev, created].sort((a, b) => a.name.localeCompare(b.name)));
      setSelectedName(created.name);
      if (compactLayout) setSidebarOpen(false);
      setCreateName("");
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleSave = async () => {
    if (!draft) return;
    setError(null);
    setCompileOutput(null);

    let updated: Pipeline;

    if (mode === "python") {
      if (!pythonText.trim()) {
        setError("Python source is required in Python mode.");
        return;
      }
      try {
        const compiled = await compilePipelinePython({
          ...draft,
          editor_mode: "python",
          python_source: pythonText,
        });
        setGraphText(jsonPretty(compiled.graph ?? emptyGraph()));
        updated = {
          ...draft,
          graph: compiled.graph,
          editor_mode: "python",
          python_source: pythonText,
        };
      } catch (err: any) {
        setError(String(err?.message ?? err));
        return;
      }
    } else {
      const resolved = resolveGraphFromActiveMode();
      if (!resolved.ok) {
        setError(resolved.message);
        return;
      }
      updated = {
        ...draft,
        graph: resolved.graph,
        editor_mode: mode,
        python_source: draft.python_source ?? "",
      };
    }

    try {
      const saved = await putPipeline(draft.name, updated);
      setPipelines((prev) =>
        prev.map((pipeline) => (pipeline.name === saved.name ? saved : pipeline)).sort((a, b) => a.name.localeCompare(b.name)),
      );
      setDraft(saved);
      setGraphText(jsonPretty(saved.graph ?? emptyGraph()));
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleCompile = async () => {
    if (!draft) return;
    setError(null);
    setCompileOutput(null);

    try {
      if (mode === "python") {
        if (!pythonText.trim()) {
          setError("Python source is required in Python mode.");
          return;
        }
        const output = await compilePipelinePython({
          ...draft,
          editor_mode: "python",
          python_source: pythonText,
        });
        setCompileOutput(output);
        setGraphText(jsonPretty(output.graph ?? emptyGraph()));
        return;
      }
      const resolved = resolveGraphFromActiveMode();
      if (!resolved.ok) {
        setError(resolved.message);
        return;
      }
      const output = await compilePipeline({ ...draft, graph: resolved.graph });
      setCompileOutput(output);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleDuplicate = useCallback(
    async (newName: string) => {
      if (!draft) return;
      const created = await duplicatePipeline(draft.name, newName);
      setPipelines((prev) => [...prev, created].sort((a, b) => a.name.localeCompare(b.name)));
      setSelectedName(created.name);
      if (compactLayout) setSidebarOpen(false);
    },
    [draft, compactLayout],
  );

  const handleDelete = async () => {
    if (!draft) return;
    if (!confirm(t("core.ui.pipelines.confirm_delete", { name: draft.name }))) return;
    setError(null);
    try {
      await deletePipeline(draft.name);
      setPipelines((prev) => prev.filter((pipeline) => pipeline.name !== draft.name));
      setSelectedName(null);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleApplyTemplate = async (
    payload: PipelineTemplateApplyCamerasRequest,
  ): Promise<PipelineTemplateApplyCamerasResponse> => {
    const res = await applyPipelineTemplateToCameras(payload);
    await reload();
    const next = (res.created ?? [])[0] ?? (res.updated ?? [])[0] ?? null;
    if (next) setSelectedName(next);
    return res;
  };

  return (
    <div className="pipelinesRoot screenRoot">
      <div className="pipelinesTopbar">
        <button className="iconButton" type="button" onClick={onClose} aria-label={t("core.actions.back", {}, "Back")}>
          <i className="fa-solid fa-arrow-left" aria-hidden="true" />
        </button>
        <div className="pipelinesTitle">{t("core.ui.pipelines.title")}</div>
        <div className="pipelinesTopbarRight">
          {isAggregateHome ? (
            <div className="pipelinesFlag">{t("core.ui.pipelines.telemetry.aggregate.scope", {}, "All pipelines")}</div>
          ) : draft ? (
            <div className="pipelinesFlag">{draft.name}</div>
          ) : null}
          {compactLayout ? (
            <button
              className={["iconButton", sidebarOpen ? "isActive" : ""].filter(Boolean).join(" ")}
              type="button"
              onClick={() => setSidebarOpen((prev) => !prev)}
              aria-label={t("core.ui.pipelines.aria.toggle_list")}
              title={t("core.ui.pipelines.aria.toggle_list")}
            >
              <i className="fa-solid fa-list" aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>

      <div
        className={[
          "pipelinesBody",
          compactLayout ? "isCompact" : "",
          compactLayout && sidebarOpen ? "isSidebarOpen" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {compactLayout && sidebarOpen ? (
          <button
            className="pipelinesSidebarBackdrop"
            type="button"
            aria-label={t("core.ui.pipelines.aria.close_list")}
            tabIndex={-1}
            onClick={() => setSidebarOpen(false)}
          />
        ) : null}
        <div className="pipelinesSidebar">
          <div className="pipelinesSidebarHeader">
            <div className="pipelinesSidebarTitle">{t("core.ui.pipelines.title")}</div>
          </div>

          <div className="pipelinesCreate">
            <input
              className="pipelinesInput"
              placeholder={t("core.ui.pipelines.create.placeholder_name")}
              value={createName}
              onChange={(event) => setCreateName(event.target.value)}
            />
            <button className="pillButton" type="button" onClick={() => void handleCreate()}>
              <i className="fa-solid fa-plus" aria-hidden="true" />
              {t("core.ui.pipelines.create.button")}
            </button>
          </div>

          <div className="pipelinesList">
            {pipelines.map((pipeline) => (
              <button
                key={pipeline.name}
                type="button"
                className={["pipelinesListItem", selectedName === pipeline.name ? "isActive" : ""].filter(Boolean).join(" ")}
                onClick={() => {
                  setSelectedName(pipeline.name);
                  if (compactLayout) setSidebarOpen(false);
                }}
              >
                <div className="pipelinesListItemName">{pipeline.name}</div>
                <div className="pipelinesListItemMeta">
                  {pipeline.type === "reuse" ? t("core.ui.pipelines.type.reuse") : t("core.ui.pipelines.type.final")}
                </div>
              </button>
            ))}
          </div>

          <div className="pipelinesSidebarFooter">
            <div className="pipelinesSidebarTitle">{t("core.ui.pipelines.sidebar.processing_servers.title")}</div>
            <div className="pipelinesHint">{t("core.ui.pipelines.sidebar.processing_servers.desc")}</div>
            {onOpenProcessingServers ? (
              <button className="pillButton" type="button" onClick={onOpenProcessingServers}>
                <i className="fa-solid fa-server" aria-hidden="true" />
                {t("core.ui.pipelines.sidebar.processing_servers.manage")}
              </button>
            ) : null}
          </div>
        </div>

        <div className={["pipelinesEditor", isAggregateHome ? "pipelinesEditorAggregate" : ""].filter(Boolean).join(" ")}>
          {isAggregateHome ? (
            <>
              {error ? (
                <div className="card cardDanger">
                  <div className="cardBody">{error}</div>
                </div>
              ) : null}
              <PipelineTelemetryOverviewCard aggregate pipelineName={null} steps={[]} />
            </>
          ) : loading ? (
            <div className="card">
              <div className="cardBody">{t("core.ui.loading")}</div>
            </div>
          ) : error ? (
            <div className="card cardDanger">
              <div className="cardBody">{error}</div>
            </div>
          ) : !draft ? (
            <div className="card">
              <div className="cardBody">{t("core.ui.pipelines.empty")}</div>
            </div>
          ) : (
            <div className="pipelinesEditorInner">
              <div className="pipelinesEditorHeader">
                <div className="pipelinesEditorTitle">{draft.name}</div>
                <div className="pipelinesEditorActions">
                  {draft.type === "reuse" ? (
                    <button className="pillButton" type="button" onClick={() => setTemplateApplyOpen(true)}>
                      <i className="fa-solid fa-wand-magic-sparkles" aria-hidden="true" />
                      {t("core.ui.pipelines.actions.apply_template")}
                    </button>
                  ) : null}
                  <button className="pillButton" type="button" onClick={() => void handleCompile()}>
                    <i className="fa-solid fa-gears" aria-hidden="true" />
                    {t("core.ui.pipelines.actions.compile")}
                  </button>
                  <button className="pillButton pillButtonPrimary" type="button" onClick={() => void handleSave()}>
                    <i className="fa-solid fa-floppy-disk" aria-hidden="true" />
                    {t("core.actions.save")}
                  </button>
                  <button className="pillButton" type="button" onClick={() => setDuplicateOpen(true)}>
                    <i className="fa-solid fa-copy" aria-hidden="true" />
                    {t("core.ui.pipelines.actions.duplicate")}
                  </button>
                  <button className="pillButton pillButtonDanger" type="button" onClick={() => void handleDelete()}>
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                    {t("core.actions.delete")}
                  </button>
                </div>
              </div>

              {recommendationsError ? (
                <div className="card cardDanger">
                  <div className="cardBody">{t("core.ui.pipelines.analysis.failed", { error: recommendationsError })}</div>
                </div>
              ) : null}

              {recommendationsLoading ? <div className="pipelinesHint">{t("core.ui.pipelines.analysis.loading")}</div> : null}

              {recommendations.length > 0 ? (
                <div className="card">
                  <div className="cardTitle">{t("core.ui.pipelines.recommendations.title")}</div>
                  <div className="cardBody">
                    <div className="pipelinesAlerts">
                      {recommendations.map((alert, index) => (
                        <div
                          key={`${alert.code}:${alert.node_id ?? ""}:${index}`}
                          className={["pipelinesAlertRow", alert.severity === "warning" ? "isWarning" : "isInfo"]
                            .filter(Boolean)
                            .join(" ")}
                        >
                          <div className="pipelinesAlertBadge">{alert.severity}</div>
                          <div className="pipelinesAlertText">
                            <div className="pipelinesAlertMessage">{alert.message}</div>
                            {alert.suggestion ? <div className="pipelinesAlertSuggestion">{alert.suggestion}</div> : null}
                            {alert.node_id ? <div className="pipelinesHint">{t("core.ui.pipelines.recommendations.node", { node_id: alert.node_id })}</div> : null}
                            {alert.edge ? <pre className="pipelinesPre">{JSON.stringify(alert.edge, null, 2)}</pre> : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              ) : null}

              <div className="pipelinesEditorGrid">
                <div className="pipelinesForm">
                  <label className="pipelinesLabel">
                    <span>{t("core.ui.pipelines.form.type")}</span>
                    <select
                      className="pipelinesSelect"
                      value={draft.type}
                      onChange={(event) => setDraft((prev) => (prev ? { ...prev, type: event.target.value as any } : prev))}
                      disabled={isPythonLocked}
                    >
                      <option value="final">{t("core.ui.pipelines.type.final")}</option>
                      <option value="reuse">{t("core.ui.pipelines.type.reuse")}</option>
                    </select>
                  </label>

                  {draft.type === "final" ? (
                    <>
                      <label className="pipelinesLabel">
                        <span>{t("core.ui.pipelines.form.enabled")}</span>
                        <input
                          type="checkbox"
                          checked={draft.enabled !== false}
                          onChange={(event) => setDraft((prev) => (prev ? { ...prev, enabled: event.target.checked } : prev))}
                        />
                      </label>

                      <label className="pipelinesLabel">
                        <span>{t("core.ui.pipelines.form.processing_server")}</span>
                        <div className="row">
                          <select
                            className="pipelinesSelect"
                            value={draft.processing_server_id ?? "local"}
                            onChange={(event) =>
                              setDraft((prev) => (prev ? { ...prev, processing_server_id: event.target.value } : prev))
                            }
                          >
                            {servers.map((server) => (
                              <option key={server.id} value={server.id}>
                                {server.id}
                              </option>
                            ))}
                          </select>
                          {onOpenProcessingServers ? (
                            <button className="pillButton" type="button" onClick={onOpenProcessingServers}>
                              {t("core.ui.pipelines.form.processing_server.manage")}
                            </button>
                          ) : null}
                        </div>
                      </label>
                    </>
                  ) : null}

                  <div className="pipelinesModes">
                    <button
                      className={["pillButton", mode === "interactive" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      disabled={isPythonLocked}
                      onClick={() => switchMode("interactive")}
                    >
                      {t("core.ui.pipelines.modes.interactive")}
                    </button>
                    <button
                      className={["pillButton", mode === "json" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      disabled={isPythonLocked}
                      onClick={() => switchMode("json")}
                    >
                      {t("core.ui.pipelines.modes.json")}
                    </button>
                    <button
                      className={["pillButton", mode === "python" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      onClick={() => switchMode("python")}
                    >
                      {t("core.ui.pipelines.modes.python_one_way")}
                    </button>
                  </div>

                  <div className="pipelinesHint">{t("core.ui.pipelines.operator_count", { count: operators.length })}</div>
                </div>

                <div className="pipelinesEditorPanel">
                  {mode === "python" ? (
                    <div className="pipelinesMonacoWrap">
                      <Editor
                        height="520px"
                        language="python"
                        value={pythonText}
                        onChange={(value) => setPythonText(String(value ?? ""))}
                        options={{
                          automaticLayout: true,
                          fontSize: 13,
                          minimap: { enabled: false },
                          scrollBeyondLastLine: false,
                          wordWrap: "on",
                        }}
                      />
                    </div>
                  ) : mode === "interactive" ? (
                    <InteractivePipelineEditor
                      operatorsById={operatorsById}
                      camerasIndex={camerasIndex}
                      pipelineName={draft?.name ?? null}
                      processingServerId={draft?.processing_server_id ?? "local"}
                      stepOutputsByNodeId={stepOutputsByNodeId}
                      interactiveSteps={interactiveSteps}
                      setInteractiveSteps={setInteractiveSteps}
                      interactiveWarning={interactiveWarning}
                      setInteractiveWarning={setInteractiveWarning}
                      interactiveGraph={interactiveGraph}
                      onOpenTelemetryField={openTelemetryFieldInspector}
                    />
                  ) : (
                    <div className="pipelinesMonacoWrap">
                      <Editor
                        height="520px"
                        language="json"
                        value={graphText}
                        onChange={(value) => setGraphText(String(value ?? ""))}
                        options={{
                          automaticLayout: true,
                          fontSize: 13,
                          minimap: { enabled: false },
                          scrollBeyondLastLine: false,
                          wordWrap: "on",
                        }}
                      />
                    </div>
                  )}
                </div>
              </div>

              {compileOutput ? (
                <div className="card">
                  <div className="cardTitle">{t("core.ui.pipelines.compile_output.title")}</div>
                  <div className="cardBody">
                    <pre className="pipelinesPre">{JSON.stringify(compileOutput, null, 2)}</pre>
                  </div>
                </div>
              ) : null}

                <PipelineTelemetryOverviewCard
                  pipelineName={draft?.name ?? null}
                  steps={interactiveSteps}
                  externalRefreshNonce={telemetryResetNonce}
                  resetting={telemetryResetting}
                  onReset={resetTelemetryAndStats}
                />
              </div>
            )}
        </div>
      </div>

	      <PipelineTelemetryFieldModal
	        open={Boolean(telemetryFieldInspector && draft)}
	        pipelineName={draft?.name ?? null}
	        request={telemetryFieldInspector}
	        refreshNonce={telemetryResetNonce}
	        onClose={() => setTelemetryFieldInspector(null)}
	        onApplyValue={(value) => void applyTelemetryFieldValue(value)}
	      />

      <PipelineDuplicateModal
        open={duplicateOpen}
        pipeline={draft}
        existingNames={pipelines.map((pipeline) => pipeline.name)}
        onClose={() => setDuplicateOpen(false)}
        onDuplicate={handleDuplicate}
      />

      <PipelineTemplateApplyModal
        open={templateApplyOpen}
        template={draft?.type === "reuse" ? draft : null}
        cameras={camerasIndex.cameras}
        servers={servers}
        onClose={() => setTemplateApplyOpen(false)}
        onApply={handleApplyTemplate}
      />
    </div>
  );
}
