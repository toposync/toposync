import React, { useCallback, useEffect, useMemo, useState } from "react";

import type { ProcessingServer, ProcessingServerStatus } from "../../util/api";
import { deleteProcessingServer, getProcessingServerStatus, listProcessingServers, putProcessingServer } from "../../util/api";
import { i18n } from "../../util/i18n";
import { ProcessingServerModal } from "../ProcessingServerModal";

type Props = {
  onClose: () => void;
};

function sortServers(list: ProcessingServer[]): ProcessingServer[] {
  const local = list.find((s) => s.id === "local") ?? null;
  const rest = list.filter((s) => s.id !== "local").sort((a, b) => a.id.localeCompare(b.id));
  return local ? [local, ...rest] : rest;
}

export function ProcessingServersScreen({ onClose }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [servers, setServers] = useState<ProcessingServer[]>([]);
  const [serverModalOpen, setServerModalOpen] = useState(false);
  const [serverModalTarget, setServerModalTarget] = useState<ProcessingServer | null>(null);
  const [serverStatusById, setServerStatusById] = useState<Record<string, ProcessingServerStatus>>({});
  const [serverTestingById, setServerTestingById] = useState<Record<string, boolean>>({});

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await listProcessingServers();
      setServers(sortServers(list));
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const canAdd = useMemo(() => servers.length < 128, [servers.length]);

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
    if (confirmDelete && !confirm(t("core.ui.processing_servers.confirm_delete", { id: serverId }))) return;
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

  return (
    <div className="pipelinesRoot screenRoot">
      <div className="pipelinesTopbar">
        <button className="iconButton" type="button" onClick={onClose} aria-label={t("core.actions.back", {}, "Back")}>
          <i className="fa-solid fa-arrow-left" aria-hidden="true" />
        </button>
        <div className="pipelinesTitle">{t("core.ui.processing_servers.title")}</div>
        <div className="pipelinesTopbarRight">
          <button
            className="pillButton pillButtonPrimary"
            type="button"
            disabled={!canAdd}
            onClick={() => {
              setServerModalTarget(null);
              setServerModalOpen(true);
            }}
          >
            <i className="fa-solid fa-plus" aria-hidden="true" />
            {t("core.ui.processing_servers.add_server")}
          </button>
        </div>
      </div>

      <div className="processingServersBody">
        <div className="card">
          <div className="cardBody">{t("core.ui.processing_servers.description")}</div>
        </div>

        {loading ? (
          <div className="card">
            <div className="cardBody">{t("core.ui.loading")}</div>
          </div>
        ) : null}

        {error ? (
          <div className="card cardDanger">
            <div className="cardBody">{error}</div>
          </div>
        ) : null}

        <div className="processingServersList">
          {servers.map((server) => {
            const probe = serverStatusById[server.id] ?? null;
            const testing = !!serverTestingById[server.id];
            const statusLabel = testing
              ? ` • ${t("core.ui.processing_servers.status.testing")}`
              : probe
                ? probe.ok
                  ? ` • ${t("core.ui.processing_servers.status.online")}`
                  : ` • ${t("core.ui.processing_servers.status.offline")}`
                : "";
            const statusTitle = testing ? t("core.ui.processing_servers.status.testing") : probe && !probe.ok ? String(probe.error || "") : "";
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
                  <div className="pipelinesServerId">
                    {server.id}
                    {server.id === "local" ? ` ${t("core.ui.processing_servers.built_in")}` : ""}
                  </div>
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
                    title={t("core.ui.processing_servers.actions.test_connection")}
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
                      title={t("core.ui.processing_servers.actions.edit_server")}
                    >
                      <i className="fa-solid fa-pen-to-square" aria-hidden="true" />
                    </button>

                    <button
                      className="iconButton iconButtonDanger"
                      type="button"
                      onClick={() => void handleDeleteServer(server.id)}
                      title={t("core.ui.processing_servers.actions.delete_server")}
                    >
                      <i className="fa-solid fa-trash" aria-hidden="true" />
                    </button>
                  </>
                ) : null}
              </div>
            );
          })}

          {!loading && servers.length === 0 ? (
            <div className="card">
              <div className="cardBody">{t("core.ui.processing_servers.none")}</div>
            </div>
          ) : null}
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
    </div>
  );
}
