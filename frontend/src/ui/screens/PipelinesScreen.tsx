import React, { useCallback, useEffect, useMemo, useState } from "react";

import { i18n } from "../../util/i18n";
import type { Pipeline, ProcessingServer } from "../../util/api";
import {
  compilePipeline,
  createPipeline,
  deletePipeline,
  deleteProcessingServer,
  getPipelinesFeatureFlag,
  listPipelines,
  listProcessingServers,
  putPipeline,
  putProcessingServer,
  setPipelinesFeatureFlag,
} from "../../util/api";

type Props = {
  onClose: () => void;
};

type EditorMode = "interactive" | "json" | "python";

function safeJsonParse(value: string): { ok: true; data: any } | { ok: false; error: string } {
  try {
    return { ok: true, data: JSON.parse(value) };
  } catch (err: any) {
    return { ok: false, error: String(err?.message ?? err) };
  }
}

function emptyGraph(): any {
  return { schema_version: 1, nodes: [], edges: [] };
}

function defaultPipeline(name: string, type: "reuse" | "final"): Pipeline {
  return {
    name,
    type,
    enabled: true,
    processing_server_id: "local",
    editor_mode: "json",
    python_source: "",
    graph: emptyGraph(),
  };
}

export function PipelinesScreen({ onClose }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [servers, setServers] = useState<ProcessingServer[]>([]);
  const [featureFlag, setFeatureFlag] = useState<boolean>(false);
  const [selectedName, setSelectedName] = useState<string | null>(null);

  const [createName, setCreateName] = useState("");
  const [createType, setCreateType] = useState<"reuse" | "final">("final");

  const selected = useMemo(() => {
    if (!selectedName) return null;
    return pipelines.find((p) => p.name === selectedName) ?? null;
  }, [pipelines, selectedName]);

  const [draft, setDraft] = useState<Pipeline | null>(null);
  const [graphText, setGraphText] = useState<string>("");
  const [pythonText, setPythonText] = useState<string>("");
  const [mode, setMode] = useState<EditorMode>("json");
  const [compileOutput, setCompileOutput] = useState<any>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [flag, pls, svs] = await Promise.all([getPipelinesFeatureFlag(), listPipelines(), listProcessingServers()]);
      setFeatureFlag(Boolean(flag?.enabled));
      setPipelines(pls);
      setServers(svs);
      if (!selectedName && pls.length > 0) setSelectedName(pls[0].name);
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
      setMode("json");
      setCompileOutput(null);
      return;
    }
    setDraft(selected);
    setGraphText(JSON.stringify(selected.graph ?? emptyGraph(), null, 2));
    setPythonText(String(selected.python_source ?? ""));
    setMode((selected.editor_mode as EditorMode) ?? "json");
    setCompileOutput(null);
  }, [selected]);

  const isPythonLocked = Boolean(draft && draft.editor_mode === "python");

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

    let graph: any = draft.graph ?? emptyGraph();
    if (mode === "json" || mode === "interactive") {
      const parsed = safeJsonParse(graphText);
      if (!parsed.ok) {
        setError(`Invalid graph JSON: ${parsed.error}`);
        return;
      }
      graph = parsed.data;
    }

    const updated: Pipeline = {
      ...draft,
      graph,
      editor_mode: mode,
      python_source: mode === "python" ? pythonText : draft.python_source ?? "",
    };

    try {
      const saved = await putPipeline(draft.name, updated);
      setPipelines((prev) => prev.map((p) => (p.name === saved.name ? saved : p)).sort((a, b) => a.name.localeCompare(b.name)));
      setDraft(saved);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleCompile = async () => {
    if (!draft) return;
    setError(null);
    setCompileOutput(null);

    const parsed = safeJsonParse(graphText);
    if (!parsed.ok) {
      setError(`Invalid graph JSON: ${parsed.error}`);
      return;
    }

    try {
      const output = await compilePipeline({ ...draft, graph: parsed.data });
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
      setPipelines((prev) => prev.filter((p) => p.name !== draft.name));
      setSelectedName(null);
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleToggleFlag = async () => {
    setError(null);
    try {
      const next = await setPipelinesFeatureFlag(!featureFlag);
      setFeatureFlag(Boolean(next?.enabled));
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

  const handleDeleteServer = async (serverId: string) => {
    if (!confirm(`Delete processing server '${serverId}'?`)) return;
    setError(null);
    try {
      await deleteProcessingServer(serverId);
      await reload();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  return (
    <div className="pipelinesRoot screenRoot">
      <div className="pipelinesTopbar">
        <button className="iconButton" type="button" onClick={onClose} aria-label={t("core.actions.back", {}, "Back")}>
          <i className="fa-solid fa-arrow-left" aria-hidden="true" />
        </button>
        <div className="pipelinesTitle">Pipelines</div>
        <div className="pipelinesTopbarRight">
          <label className="pipelinesFlag">
            <input type="checkbox" checked={featureFlag} onChange={() => void handleToggleFlag()} />
            <span>Enable pipelines</span>
          </label>
        </div>
      </div>

      <div className="pipelinesBody">
        <div className="pipelinesSidebar">
          <div className="pipelinesSidebarHeader">
            <div className="pipelinesSidebarTitle">Pipelines</div>
          </div>

          <div className="pipelinesCreate">
            <input
              className="pipelinesInput"
              placeholder="pipeline_name"
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
            />
            <select className="pipelinesSelect" value={createType} onChange={(e) => setCreateType(e.target.value as any)}>
              <option value="final">final</option>
              <option value="reuse">reuse</option>
            </select>
            <button className="pillButton" type="button" onClick={() => void handleCreate()}>
              Create
            </button>
          </div>

          <div className="pipelinesList">
            {pipelines.map((p) => (
              <button
                key={p.name}
                type="button"
                className={["pipelinesListItem", selectedName === p.name ? "isActive" : ""].filter(Boolean).join(" ")}
                onClick={() => setSelectedName(p.name)}
              >
                <div className="pipelinesListItemName">{p.name}</div>
                <div className="pipelinesListItemMeta">{p.type}</div>
              </button>
            ))}
          </div>

          <div className="pipelinesSidebarFooter">
            <div className="pipelinesSidebarTitle">Processing</div>
            <div className="pipelinesServers">
              {servers.map((s) => (
                <div key={s.id} className="pipelinesServerRow">
                  <div className="pipelinesServerMain">
                    <div className="pipelinesServerId">{s.id}</div>
                    <div className="pipelinesServerMeta">
                      {s.kind}
                      {s.url ? ` • ${s.url}` : ""}
                    </div>
                  </div>
                  {s.id !== "local" ? (
                    <button className="iconButton" type="button" onClick={() => void handleDeleteServer(s.id)} title="Delete server">
                      <i className="fa-solid fa-trash" aria-hidden="true" />
                    </button>
                  ) : null}
                </div>
              ))}
            </div>

            <button
              className="pillButton"
              type="button"
              onClick={() =>
                void handleSaveServer({
                  id: `srv_${Math.random().toString(16).slice(2, 8)}`,
                  name: "",
                  kind: "http",
                  url: "http://127.0.0.1:9001",
                })
              }
            >
              Add HTTP server (stub)
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
                  <button className="pillButton" type="button" onClick={() => void handleCompile()}>
                    Compile
                  </button>
                  <button className="pillButton pillButtonPrimary" type="button" onClick={() => void handleSave()}>
                    Save
                  </button>
                  <button className="pillButton pillButtonDanger" type="button" onClick={() => void handleDelete()}>
                    Delete
                  </button>
                </div>
              </div>

              <div className="pipelinesEditorGrid">
                <div className="pipelinesForm">
                  <label className="pipelinesLabel">
                    <span>Type</span>
                    <select
                      className="pipelinesSelect"
                      value={draft.type}
                      onChange={(e) => setDraft((prev) => (prev ? { ...prev, type: e.target.value as any } : prev))}
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
                          onChange={(e) => setDraft((prev) => (prev ? { ...prev, enabled: e.target.checked } : prev))}
                        />
                      </label>

                      <label className="pipelinesLabel">
                        <span>Processing server</span>
                        <select
                          className="pipelinesSelect"
                          value={draft.processing_server_id ?? "local"}
                          onChange={(e) =>
                            setDraft((prev) => (prev ? { ...prev, processing_server_id: e.target.value } : prev))
                          }
                        >
                          {servers.map((s) => (
                            <option key={s.id} value={s.id}>
                              {s.id}
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
                      onClick={() => setMode("interactive")}
                    >
                      Interactive
                    </button>
                    <button
                      className={["pillButton", mode === "json" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      disabled={isPythonLocked}
                      onClick={() => setMode("json")}
                    >
                      JSON
                    </button>
                    <button
                      className={["pillButton", mode === "python" ? "isActive" : ""].filter(Boolean).join(" ")}
                      type="button"
                      onClick={() => setMode("python")}
                    >
                      Python (one-way)
                    </button>
                  </div>
                </div>

                <div className="pipelinesEditorPanel">
                  {mode === "python" ? (
                    <textarea
                      className="pipelinesTextarea"
                      value={pythonText}
                      onChange={(e) => setPythonText(e.target.value)}
                      placeholder="# Pipeline DSL (stored as python_source). Execution still uses graph for now."
                    />
                  ) : mode === "interactive" ? (
                    <div className="card">
                      <div className="cardBody">
                        Interactive editor is not implemented yet. Use JSON mode for now.
                      </div>
                    </div>
                  ) : (
                    <textarea
                      className="pipelinesTextarea"
                      value={graphText}
                      onChange={(e) => setGraphText(e.target.value)}
                      spellCheck={false}
                    />
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
    </div>
  );
}

