import React, { useEffect, useMemo, useState } from "react";

import type { ProcessingServer, ProcessingServerStatus } from "../util/api";
import { i18n } from "../util/i18n";
import { Modal } from "./Modal";

type Props = {
  open: boolean;
  server: ProcessingServer | null;
  onClose: () => void;
  onSave: (server: ProcessingServer) => Promise<void>;
  onDelete: (serverId: string) => Promise<void>;
  onTest: (serverId: string) => Promise<ProcessingServerStatus>;
};

type UrlParts = {
  scheme: "http" | "https";
  host: string;
  port: string;
};

function parseUrlParts(url: string): UrlParts {
  const trimmed = String(url || "").trim();
  if (!trimmed) return { scheme: "http", host: "", port: "9001" };
  try {
    const parsed = new URL(trimmed);
    const scheme = parsed.protocol.replace(":", "") === "https" ? "https" : "http";
    const host = parsed.hostname || "";
    const port = parsed.port || (scheme === "https" ? "443" : "80");
    return { scheme, host, port };
  } catch {
    return { scheme: "http", host: "", port: "9001" };
  }
}

function makeSuggestedId(host: string, port: string): string {
  const safeHost = String(host || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  const safePort = String(port || "").trim().replace(/[^0-9]+/g, "");
  const base = [safeHost, safePort].filter(Boolean).join("_");
  const raw = base ? `remote_${base}` : "remote_server";
  return raw.slice(0, 64);
}

function buildUrl(scheme: string, host: string, port: string): string {
  const schemeTrimmed = String(scheme || "http").trim().toLowerCase() === "https" ? "https" : "http";
  const hostTrimmed = String(host || "").trim();
  const portTrimmed = String(port || "").trim();
  if (!hostTrimmed || !portTrimmed) return "";
  return `${schemeTrimmed}://${hostTrimmed}:${portTrimmed}`;
}

export function ProcessingServerModal({ open, server, onClose, onSave, onDelete, onTest }: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const editing = server != null;
  const parts = useMemo(() => parseUrlParts(server?.url ?? ""), [server?.url]);

  const [serverId, setServerId] = useState("");
  const [name, setName] = useState("");
  const [scheme, setScheme] = useState<"http" | "https">("http");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("9001");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ProcessingServerStatus | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLocalError(null);
    setTestResult(null);

    if (!server) {
      setServerId("");
      setName("");
      setScheme("http");
      setHost("");
      setPort("9001");
      setUsername("");
      setPassword("");
      return;
    }

    setServerId(server.id);
    setName(String(server.name || ""));
    setScheme(parts.scheme);
    setHost(parts.host);
    setPort(parts.port);
    setUsername(String(server.username || ""));
    setPassword(String(server.password || ""));
  }, [open, server, parts.scheme, parts.host, parts.port]);

  const urlPreview = useMemo(() => buildUrl(scheme, host, port), [scheme, host, port]);

  const canSave = useMemo(() => {
    const id = serverId.trim();
    if (!id) return false;
    if (id === "local") return false;
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(id)) return false;
    if (!urlPreview) return false;
    const portNum = Number.parseInt(port, 10);
    if (!Number.isFinite(portNum) || portNum <= 0 || portNum > 65535) return false;
    return true;
  }, [serverId, urlPreview, port]);

  const suggestedId = useMemo(() => makeSuggestedId(host, port), [host, port]);

  useEffect(() => {
    if (!open) return;
    if (editing) return;
    if (serverId.trim()) return;
    if (!host.trim() || !port.trim()) return;
    setServerId(suggestedId);
  }, [open, editing, serverId, host, port, suggestedId]);

  const showSuggestedIdHint = useMemo(() => {
    if (editing) return false;
    if (serverId.trim()) return false;
    if (!host.trim()) return false;
    return true;
  }, [editing, serverId, host]);

  const processingServeCommand = useMemo(() => {
    const portNum = Number.parseInt(port, 10);
    const effectivePort = Number.isFinite(portNum) && portNum > 0 ? portNum : 9001;
    const env: string[] = [];
    if (username.trim() || password.trim()) {
      env.push(`TOPOSYNC_PROCESSING_USERNAME=${JSON.stringify(username.trim())}`);
      env.push(`TOPOSYNC_PROCESSING_PASSWORD=${JSON.stringify(password.trim())}`);
    }
    const prefix = env.length ? `${env.join(" ")} ` : "";
    return `${prefix}toposync processing-serve --host 0.0.0.0 --port ${effectivePort}`;
  }, [port, username, password]);

  const saveNow = async () => {
    setLocalError(null);
    setTestResult(null);
    if (!canSave) return;
    setSaving(true);
    try {
      await onSave({
        id: serverId.trim().toLowerCase(),
        name: name.trim(),
        kind: "http",
        url: urlPreview,
        username: username.trim(),
        password: password.trim(),
      });
      onClose();
    } catch (err: any) {
      setLocalError(String(err?.message ?? err));
    } finally {
      setSaving(false);
    }
  };

  const testNow = async () => {
    if (!serverId.trim() || serverId.trim() === "local") return;
    setLocalError(null);
    setTesting(true);
    try {
      const status = await onTest(serverId.trim().toLowerCase());
      setTestResult(status);
    } catch (err: any) {
      setTestResult({ ok: false, error: String(err?.message ?? err) });
    } finally {
      setTesting(false);
    }
  };

  const deleteNow = async () => {
    if (!serverId.trim() || serverId.trim() === "local") return;
    if (!confirm(`Delete processing server '${serverId.trim()}'?`)) return;
    setLocalError(null);
    setSaving(true);
    try {
      await onDelete(serverId.trim().toLowerCase());
      onClose();
    } catch (err: any) {
      setLocalError(String(err?.message ?? err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} title={editing ? "Edit processing server" : "Add processing server"} onClose={onClose}>
      {localError ? (
        <div className="card cardDanger">
          <div className="cardBody">{localError}</div>
        </div>
      ) : null}

      <div className="pipelinesHint">
        Run the processing server on another machine and connect it here. Storage and notifications still happen on the origin server.
      </div>

      <div className="processingServerForm">
        <label className="pipelinesLabel">
          <span>ID</span>
          <input
            className="pipelinesInput"
            value={serverId}
            onChange={(event) => setServerId(event.target.value)}
            placeholder={suggestedId}
            disabled={editing}
          />
        </label>

        {showSuggestedIdHint ? <div className="pipelinesHint">Suggested id: {suggestedId}</div> : null}

        <label className="pipelinesLabel">
          <span>Name (optional)</span>
          <input className="pipelinesInput" value={name} onChange={(event) => setName(event.target.value)} placeholder="Garage GPU" />
        </label>

        <div className="processingServerEndpointRow">
          <label className="pipelinesLabel">
            <span>Scheme</span>
            <select className="pipelinesSelect" value={scheme} onChange={(event) => setScheme(event.target.value as any)}>
              <option value="http">http</option>
              <option value="https">https</option>
            </select>
          </label>

          <label className="pipelinesLabel">
            <span>Host / IP</span>
            <input className="pipelinesInput" value={host} onChange={(event) => setHost(event.target.value)} placeholder="192.168.1.50" />
          </label>

          <label className="pipelinesLabel">
            <span>Port</span>
            <input className="pipelinesInput" value={port} onChange={(event) => setPort(event.target.value)} placeholder="9001" />
          </label>
        </div>

        <div className="pipelinesHint">URL preview: {urlPreview || "—"}</div>

        <div className="processingServerAuthRow">
          <label className="pipelinesLabel">
            <span>Username (optional)</span>
            <input className="pipelinesInput" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="mateus" />
          </label>

          <label className="pipelinesLabel">
            <span>Password (optional)</span>
            <input
              className="pipelinesInput"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="••••••••"
              autoComplete="new-password"
            />
          </label>
        </div>

        <div className="pipelinesHint">
          Remote command: <pre className="pipelinesPre">{processingServeCommand}</pre>
        </div>
      </div>

      {testResult ? (
        <div className={["card", testResult.ok ? "" : "cardDanger"].filter(Boolean).join(" ")}>
          <div className="cardBody">
            {testResult.ok ? (
              <>
                <div>Connection: OK</div>
                <pre className="pipelinesPre">{JSON.stringify(testResult.status ?? {}, null, 2)}</pre>
              </>
            ) : (
              <div>Connection: {testResult.error || "failed"}</div>
            )}
          </div>
        </div>
      ) : null}

      <div className="modalFooter">
        {editing ? (
          <button className="pillButton pillButtonDanger" type="button" disabled={saving} onClick={() => void deleteNow()}>
            Delete
          </button>
        ) : null}

        <button className="pillButton" type="button" disabled={testing || saving || !editing} onClick={() => void testNow()}>
          {testing ? t("core.ui.loading") : "Test connection"}
        </button>

        <button className="pillButton pillButtonPrimary" type="button" disabled={saving || !canSave} onClick={() => void saveNow()}>
          {saving ? t("core.ui.loading") : "Save"}
        </button>
      </div>
    </Modal>
  );
}
