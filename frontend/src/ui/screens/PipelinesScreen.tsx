import Editor from "@monaco-editor/react";
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { i18n } from "../../util/i18n";
import type {
  CamerasIndexResponse,
  PipelineTemplateApplyCamerasRequest,
  PipelineTemplateApplyCamerasResponse,
  Pipeline,
  PipelineAlert,
  PipelineCompileOutput,
  PipelineCompilePythonOutput,
  PipelineOperatorDefinition,
  ProcessingServer,
  ProcessingServerStatus,
} from "../../util/api";
import {
  applyPipelineTemplateToCameras,
  compilePipeline,
  compilePipelinePython,
  createPipeline,
  deletePipeline,
  deleteProcessingServer,
  getProcessingServerStatus,
  listCamerasIndex,
  listPipelineOperators,
  listPipelines,
  listProcessingServers,
  putPipeline,
  putProcessingServer,
} from "../../util/api";
import { ProcessingServerModal } from "../ProcessingServerModal";
import { InteractivePipelineEditor } from "./pipelines/InteractivePipelineEditor";
import { PipelineTemplateApplyModal } from "./pipelines/PipelineTemplateApplyModal";
import type { EditorMode, InteractiveStep } from "./pipelines/types";
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
};

export function PipelinesScreen({ onClose }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [servers, setServers] = useState<ProcessingServer[]>([]);
  const [serverModalOpen, setServerModalOpen] = useState(false);
  const [serverModalTarget, setServerModalTarget] = useState<ProcessingServer | null>(null);
  const [templateApplyOpen, setTemplateApplyOpen] = useState(false);
  const [serverStatusById, setServerStatusById] = useState<Record<string, ProcessingServerStatus>>({});
  const [serverTestingById, setServerTestingById] = useState<Record<string, boolean>>({});
  const [operators, setOperators] = useState<PipelineOperatorDefinition[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [camerasIndex, setCamerasIndex] = useState<CamerasIndexResponse>({ cameras: [] });

  const [createName, setCreateName] = useState("");
  const [createType, setCreateType] = useState<"reuse" | "final">("final");

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
      if (!selectedName && pipelineList.length > 0) setSelectedName(pipelineList[0].name);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setLoading(false);
    }
  }, [selectedName]);

  useEffect(() => {
    void reload();
  }, [reload]);

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

  const handleCreate = async () => {
    const name = createName.trim();
    if (!name) return;
    setError(null);
    try {
      const created = await createPipeline(defaultPipeline(name, createType));
      setPipelines((prev) => [...prev, created].sort((a, b) => a.name.localeCompare(b.name)));
      setSelectedName(created.name);
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

  const handleDelete = async () => {
    if (!draft) return;
    if (!confirm(`Delete pipeline '${draft.name}'?`)) return;
    setError(null);
    try {
      await deletePipeline(draft.name);
      setPipelines((prev) => prev.filter((pipeline) => pipeline.name !== draft.name));
      setSelectedName(null);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleSaveServer = async (server: ProcessingServer) => {
    setError(null);
    try {
      await putProcessingServer(server);
      await reload();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleDeleteServer = async (serverId: string, confirmDelete = true) => {
    if (confirmDelete && !confirm(`Delete processing server '${serverId}'?`)) return;
    setError(null);
    try {
      await deleteProcessingServer(serverId);
      await reload();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleTestServer = async (serverId: string): Promise<ProcessingServerStatus> => {
    const sid = String(serverId || "").trim().toLowerCase();
    if (!sid) return { ok: false, error: "Missing server id" };
    setServerTestingById((prev) => ({ ...prev, [sid]: true }));
    try {
      const status = await getProcessingServerStatus(sid);
      setServerStatusById((prev) => ({ ...prev, [sid]: status }));
      return status;
    } catch (err: any) {
      const failed = { ok: false, error: String(err?.message ?? err) };
      setServerStatusById((prev) => ({ ...prev, [sid]: failed }));
      return failed;
    } finally {
      setServerTestingById((prev) => ({ ...prev, [sid]: false }));
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
        <div className="pipelinesTitle">Pipelines</div>
      </div>

      <div className="pipelinesBody">
        <div className="pipelinesSidebar">
          <div className="pipelinesSidebarHeader">
            <div className="pipelinesSidebarTitle">Pipelines</div>
          </div>

          <div className="pipelinesCreate">
            <input
              className="pipelinesInput"
              placeholder="Pipeline name"
              value={createName}
              onChange={(event) => setCreateName(event.target.value)}
            />
            <select className="pipelinesSelect" value={createType} onChange={(event) => setCreateType(event.target.value as any)}>
              <option value="final">final</option>
              <option value="reuse">reuse</option>
            </select>
            <button className="pillButton" type="button" onClick={() => void handleCreate()}>
              <i className="fa-solid fa-plus" aria-hidden="true" />
              Create
            </button>
          </div>

          <div className="pipelinesList">
            {pipelines.map((pipeline) => (
              <button
                key={pipeline.name}
                type="button"
                className={["pipelinesListItem", selectedName === pipeline.name ? "isActive" : ""].filter(Boolean).join(" ")}
                onClick={() => setSelectedName(pipeline.name)}
              >
                <div className="pipelinesListItemName">{pipeline.name}</div>
                <div className="pipelinesListItemMeta">{pipeline.type}</div>
              </button>
            ))}
          </div>

          <div className="pipelinesSidebarFooter">
            <div className="pipelinesSidebarTitle">Processing servers</div>
            <div className="pipelinesServers">
              {servers.map((server) => {
                const probe = serverStatusById[server.id] ?? null;
                const testing = !!serverTestingById[server.id];
                const statusLabel = testing ? " • testing…" : probe ? (probe.ok ? " • online" : " • offline") : "";
                const statusTitle = testing ? "Testing…" : probe && !probe.ok ? String(probe.error || "Offline") : "";
                return (
                  <div key={server.id} className="pipelinesServerRow">
                    <button
                      className="pipelinesServerMain"
                      type="button"
                      disabled={server.id === "local"}
                      onClick={() => {
                        if (server.id === "local") return;
                        setServerModalTarget(server);
                        setServerModalOpen(true);
                      }}
                    >
                      <div className="pipelinesServerId">{server.id}</div>
                      <div className="pipelinesServerMeta" title={statusTitle}>
                        {server.kind}
                        {server.url ? ` • ${server.url}` : ""}
                        {statusLabel}
                      </div>
                    </button>

                    {server.kind === "http" ? (
                      <button
                        className="iconButton iconButtonPrimary"
                        type="button"
                        disabled={testing}
                        onClick={() => void handleTestServer(server.id)}
                        title="Test connection"
                      >
                        <i className="fa-solid fa-plug" aria-hidden="true" />
                      </button>
                    ) : null}

                    {server.id !== "local" ? (
                      <>
                        <button
                          className="iconButton"
                          type="button"
                          onClick={() => {
                            setServerModalTarget(server);
                            setServerModalOpen(true);
                          }}
                          title="Edit server"
                        >
                          <i className="fa-solid fa-pen-to-square" aria-hidden="true" />
                        </button>

                        <button
                          className="iconButton iconButtonDanger"
                          type="button"
                          onClick={() => void handleDeleteServer(server.id)}
                          title="Delete server"
                        >
                          <i className="fa-solid fa-trash" aria-hidden="true" />
                        </button>
                      </>
                    ) : null}
                  </div>
                );
              })}
            </div>

            <button
              className="pillButton"
              type="button"
              onClick={() => {
                setServerModalTarget(null);
                setServerModalOpen(true);
              }}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
              Add processing server
            </button>
          </div>
        </div>

        <div className="pipelinesEditor">
          {loading ? (
            <div className="card">
              <div className="cardBody">Loading…</div>
            </div>
          ) : null}

          {error ? (
            <div className="card cardDanger">
              <div className="cardBody">{error}</div>
            </div>
          ) : null}

          {!draft ? (
            <div className="card">
              <div className="cardBody">Select or create a pipeline.</div>
            </div>
          ) : (
            <div className="pipelinesEditorInner">
              <div className="pipelinesEditorHeader">
                <div className="pipelinesEditorTitle">{draft.name}</div>
                <div className="pipelinesEditorActions">
                  {draft.type === "reuse" ? (
                    <button className="pillButton" type="button" onClick={() => setTemplateApplyOpen(true)}>
                      <i className="fa-solid fa-wand-magic-sparkles" aria-hidden="true" />
                      Apply template
                    </button>
                  ) : null}
                  <button className="pillButton" type="button" onClick={() => void handleCompile()}>
                    <i className="fa-solid fa-gears" aria-hidden="true" />
                    Compile
                  </button>
                  <button className="pillButton pillButtonPrimary" type="button" onClick={() => void handleSave()}>
                    <i className="fa-solid fa-floppy-disk" aria-hidden="true" />
                    Save
                  </button>
                  <button className="pillButton pillButtonDanger" type="button" onClick={() => void handleDelete()}>
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                    Delete
                  </button>
                </div>
              </div>

              {recommendationsError ? (
                <div className="card cardDanger">
                  <div className="cardBody">Pipeline analysis failed: {recommendationsError}</div>
                </div>
              ) : null}

              {recommendationsLoading ? <div className="pipelinesHint">Analyzing pipeline…</div> : null}

              {recommendations.length > 0 ? (
                <div className="card">
                  <div className="cardTitle">Recommendations</div>
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
                            {alert.node_id ? <div className="pipelinesHint">Node: {alert.node_id}</div> : null}
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
                    <span>Type</span>
                    <select
                      className="pipelinesSelect"
                      value={draft.type}
                      onChange={(event) => setDraft((prev) => (prev ? { ...prev, type: event.target.value as any } : prev))}
                      disabled={isPythonLocked}
                    >
                      <option value="final">final</option>
                      <option value="reuse">reuse</option>
                    </select>
                  </label>

                  {draft.type === "final" ? (
                    <>
                      <label className="pipelinesLabel">
                        <span>Enabled</span>
                        <input
                          type="checkbox"
                          checked={draft.enabled !== false}
                          onChange={(event) => setDraft((prev) => (prev ? { ...prev, enabled: event.target.checked } : prev))}
                        />
                      </label>

                      <label className="pipelinesLabel">
                        <span>Processing server</span>
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
                      Interactive
                    </button>
                    <button
                      className={["pillButton", mode === "json" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      disabled={isPythonLocked}
                      onClick={() => switchMode("json")}
                    >
                      JSON
                    </button>
                    <button
                      className={["pillButton", mode === "python" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      onClick={() => switchMode("python")}
                    >
                      Python (one-way)
                    </button>
                  </div>

                  <div className="pipelinesHint">Operators available: {operators.length}</div>
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
                      interactiveSteps={interactiveSteps}
                      setInteractiveSteps={setInteractiveSteps}
                      interactiveWarning={interactiveWarning}
                      setInteractiveWarning={setInteractiveWarning}
                      interactiveGraph={interactiveGraph}
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
                  <div className="cardTitle">Compile output</div>
                  <div className="cardBody">
                    <pre className="pipelinesPre">{JSON.stringify(compileOutput, null, 2)}</pre>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </div>

      <ProcessingServerModal
        open={serverModalOpen}
        server={serverModalTarget}
        onClose={() => {
          setServerModalOpen(false);
          setServerModalTarget(null);
        }}
        onSave={handleSaveServer}
        onDelete={(serverId) => handleDeleteServer(serverId, false)}
        onTest={handleTestServer}
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
