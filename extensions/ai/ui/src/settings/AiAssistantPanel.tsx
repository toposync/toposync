import React, { useEffect, useState } from "react";
import { AiHistorySearch } from "./AiHistorySearch";
import { requestJson, type SettingsPanel } from "@toposync/plugin-api";

type Catalog = { pipelines: string[]; profiles: { id: string; name: string; model: string }[] };
type Preview = { proposal_id: string; difference: string; changes: string[]; pipeline_name: string };

export function createAiAssistantPanel(): SettingsPanel {
  return {
    id: "toposync.ai.assistant",
    name: "Assistente de pipelines",
    description: "Revisar alterações com inteligência artificial local",
    icon: "brain",
    render: () => <Assistant />,
  };
}

function Assistant(): React.ReactElement {
  const [mode, setMode] = useState("create");
  return <div style={{ display: "grid", gap: 20, minWidth: 0 }}>
    <label className="field">Tarefa<select className="input" value={mode} onChange={(event) => setMode(event.target.value)}>
      <option value="create">Criar receita</option><option value="pipeline">Alterar pipeline</option><option value="history">Consultar acontecimentos</option>
    </select></label>
    {mode === "history" ? <AiHistorySearch /> : <PipelineEditor key={mode} create={mode === "create"} />}
  </div>;
}

function PipelineEditor({ create }: { create: boolean }): React.ReactElement {
  const [catalog, setCatalog] = useState<Catalog>({ pipelines: [], profiles: [] });
  const [pipeline, setPipeline] = useState("");
  const [profile, setProfile] = useState("");
  const [instruction, setInstruction] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [undo, setUndo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    requestJson<Catalog>("/api/ai/assistant/pipelines", { signal: controller.signal }).then((value) => {
      setCatalog(value); setPipeline(value.pipelines[0] || ""); setProfile(value.profiles[0]?.id || "");
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, []);
  async function run(action: "preview" | "apply" | "undo") {
    setBusy(true); setError(""); setMessage("");
    try {
      if (action === "preview") {
        setPreview(null);
        const proposal = await requestJson<Preview & { question?: string }>(create ? "/api/ai/assistant/recipes/preview" : "/api/ai/assistant/preview", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ ...(create ? {} : { pipeline_name: pipeline }), profile_id: profile, instruction }),
        });
        if (proposal.question) setMessage(proposal.question);
        else setPreview(proposal);
      } else {
        const result = await requestJson<{ undo_id: string }>(`/api/ai/assistant/apply/${action === "undo" ? undo : preview?.proposal_id}`, { method: "POST" });
        setUndo(action === "apply" ? result.undo_id : ""); setPreview(null);
        setMessage(action === "undo" ? "Estado anterior restaurado." : "Alteração salva. Pipeline permanece desativado.");
      }
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Assistente de pipelines" style={{ display: "grid", gap: 16, maxWidth: 900, minWidth: 0, overflowWrap: "anywhere" }}>
    <p>{create ? "Descreva o que deseja observar. O assistente usa as receitas e câmeras disponíveis e pergunta quando faltar uma escolha. Revise antes de salvar; a receita começa desativada." : "Descreva uma alteração nas etapas existentes de um pipeline desativado. Revise a proposta antes de salvar. O assistente não ativa dispositivos."}</p>
    {!create && !catalog.pipelines.length && <p>Não há pipelines desativados disponíveis. Crie ou desative um pipeline no editor para começar.</p>}
    {!catalog.profiles.length && <p>Nenhum perfil com modelo Ollama instalado foi encontrado. Verifique o servidor e os perfis nas configurações de inteligência artificial.</p>}
    {!create && <label className="field">Pipeline<select className="input" value={pipeline} disabled={busy} onChange={(event) => { setPipeline(event.target.value); setPreview(null); }}>
      {catalog.pipelines.map((name) => <option key={name}>{name}</option>)}
    </select></label>}
    <details><summary>Modelo local</summary><label className="field">Perfil<select className="input" value={profile} disabled={busy} onChange={(event) => { setProfile(event.target.value); setPreview(null); }}>
      {catalog.profiles.map((item) => <option key={item.id} value={item.id}>{item.name} — {item.model}</option>)}
    </select></label></details>
    <label className="field">{create ? "O que deseja observar?" : "O que deseja alterar?"}<textarea className="input" style={{ height: "auto", padding: 10, resize: "vertical" }} rows={4} maxLength={4000} value={instruction} disabled={busy} onChange={(event) => { setInstruction(event.target.value); setPreview(null); }} placeholder={create ? "Por exemplo: avise quando um gato usar a caixa na câmera da lavanderia" : "Por exemplo: aumentar a duração mínima de presença para 30 segundos"} /></label>
    {error && <p role="alert">{error} Você também pode usar o editor de pipelines.</p>}
    <div aria-live="polite">{busy ? "Preparando alteração…" : message}</div>
    {preview ? <>
      <h3>Revisar: {preview.pipeline_name}</h3>
      <p>{create ? "Ao aplicar, a receita será criada desativada." : "Ao aplicar, somente o grafo será salvo. O pipeline permanece desativado."} Revise configurações e efeitos antes de ativá-lo no editor.</p>
      <ul>{preview.changes.map((change, index) => <li key={index}>{change}</li>)}</ul>
      <details><summary>Detalhes técnicos</summary><pre tabIndex={0} aria-label="Diferenças propostas" style={{ overflow: "auto", maxHeight: 360, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{preview.difference}</pre></details>
      <button className="primaryButton" disabled={busy} onClick={() => void run("apply")}>{create ? "Salvar receita" : "Aplicar alteração"}</button>
      <button className="chipButton" disabled={busy} onClick={() => setPreview(null)}>Descartar proposta</button>
    </> : <button className="primaryButton" disabled={busy || (!create && !pipeline) || !profile || !instruction.trim()} onClick={() => void run("preview")}>Preparar prévia</button>}
    {undo && <><button className="chipButton" disabled={busy} onClick={() => void run("undo")}>Desfazer última alteração</button><small>Disponível por uma hora, enquanto este servidor estiver em execução. Alterações concorrentes impedem a restauração.</small></>}
  </section>;
}
