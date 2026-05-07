import React, { useCallback, useEffect, useMemo, useState } from "react";

import { type AccessUsersPayload, type AuthRole, type AuthUser } from "../../util/api";
import { i18n } from "../../util/i18n";

const MVP_USER_ROLE: AuthRole = "owner";

type PairingCode = {
  code: string;
  expires_at: number;
};

type Props = {
  authUser: AuthUser | null;
  authMode: string;
  onClose: () => void;
  onLogout: () => Promise<void>;
  listAccessUsers: () => Promise<AccessUsersPayload>;
  createAccessUser: (payload: {
    username: string;
    password: string;
    role: AuthRole;
    display_name?: string;
  }) => Promise<AuthUser>;
  startAccessUserPairing: (userId: string, payload?: { device_label?: string }) => Promise<PairingCode>;
  patchAccessUser: (
    userId: string,
    payload: {
      display_name?: string;
      role?: AuthRole;
      password?: string;
      is_disabled?: boolean;
    },
  ) => Promise<AuthUser>;
  deleteAccessUser: (userId: string) => Promise<void>;
};

export function AccessScreen({
  authUser,
  authMode,
  onClose,
  onLogout,
  listAccessUsers,
  createAccessUser,
  startAccessUserPairing,
  patchAccessUser,
  deleteAccessUser,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [data, setData] = useState<AccessUsersPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");
  const [selectedUserId, setSelectedUserId] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const [displayName, setDisplayName] = useState("");
  const [isDisabled, setIsDisabled] = useState(false);
  const [newPassword, setNewPassword] = useState("");

  const [createUsername, setCreateUsername] = useState("");
  const [createDisplayName, setCreateDisplayName] = useState("");
  const [createPassword, setCreatePassword] = useState("");
  const [pairingCode, setPairingCode] = useState<PairingCode | null>(null);
  const [pairingNow, setPairingNow] = useState(() => Date.now() / 1000);
  const [pairingBusy, setPairingBusy] = useState(false);
  const [pairingError, setPairingError] = useState("");

  const canManage = authMode === "bypass" || authUser?.role === "owner";
  const formatRole = useCallback((value: AuthRole) => t(`core.ui.auth.role.${value}`, {}, value), [t]);
  const currentUserId = authUser?.id ?? "";

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await listAccessUsers();
      setData(payload);
      setSelectedUserId((prev) => {
        if (prev && payload.users.some((item) => item.id === prev)) return prev;
        if (currentUserId && payload.users.some((item) => item.id === currentUserId)) return currentUserId;
        return payload.users[0]?.id ?? "";
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setData({ users: [], grants_catalog: {} });
    } finally {
      setLoading(false);
    }
  }, [currentUserId, listAccessUsers]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const selectedUser = useMemo(() => {
    if (!data) return null;
    return data.users.find((item) => item.id === selectedUserId) ?? null;
  }, [data, selectedUserId]);

  useEffect(() => {
    if (!selectedUser) return;
    setDisplayName(selectedUser.display_name || "");
    setIsDisabled(Boolean(selectedUser.is_disabled));
    setNewPassword("");
  }, [selectedUser]);

  useEffect(() => {
    if (!pairingCode) return undefined;
    setPairingNow(Date.now() / 1000);
    const timer = window.setInterval(() => setPairingNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [pairingCode]);

  const canGeneratePairing = authMode !== "bypass" && canManage && Boolean(selectedUser) && !selectedUser?.is_disabled;
  const pairingSecondsRemaining = pairingCode ? Math.max(0, Math.ceil(pairingCode.expires_at - pairingNow)) : 0;
  const pairingExpired = Boolean(pairingCode && pairingSecondsRemaining <= 0);

  const pairingCodeChunks = useMemo(() => {
    const raw = pairingCode?.code ?? "";
    const chunks: string[] = [];
    for (let i = 0; i < raw.length; i += 4) chunks.push(raw.slice(i, i + 4));
    return chunks;
  }, [pairingCode]);

  const pairingExpiresAtLabel = useMemo(() => {
    if (!pairingCode) return "";
    return new Date(pairingCode.expires_at * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }, [pairingCode]);

  const pairingRemainingLabel = useMemo(() => {
    const total = Math.max(0, pairingSecondsRemaining);
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }, [pairingSecondsRemaining]);

  const updateUserInState = useCallback((user: AuthUser) => {
    setData((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        users: prev.users.map((item) => (item.id === user.id ? user : item)),
      };
    });
  }, []);

  const onSaveUser = useCallback(async () => {
    if (!selectedUser || !canManage || busy) return;
    setBusy(true);
    setError("");
    try {
      const payload: {
        display_name?: string;
        role?: AuthRole;
        password?: string;
        is_disabled?: boolean;
      } = {
        display_name: displayName,
        role: MVP_USER_ROLE,
        is_disabled: isDisabled,
      };
      if (newPassword.trim()) payload.password = newPassword;
      const next = await patchAccessUser(selectedUser.id, payload);
      updateUserInState(next);
      setNewPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [busy, canManage, displayName, isDisabled, newPassword, patchAccessUser, selectedUser, updateUserInState]);

  const onCreateUser = useCallback(async () => {
    if (!canManage || busy) return;
    const username = createUsername.trim();
    const password = createPassword;
    if (!username || !password) {
      setError(t("core.ui.access.error.username_password_required"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await createAccessUser({
        username,
        password,
        role: MVP_USER_ROLE,
        display_name: createDisplayName.trim(),
      });
      setData((prev) => {
        const payload = prev ?? { users: [], grants_catalog: {} };
        return { ...payload, users: [...payload.users, created] };
      });
      setSelectedUserId(created.id);
      setCreateUsername("");
      setCreateDisplayName("");
      setCreatePassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [busy, canManage, createAccessUser, createDisplayName, createPassword, createUsername, t]);

  const onStartPairing = useCallback(async () => {
    if (!selectedUser || !canGeneratePairing || pairingBusy) return;
    setPairingBusy(true);
    setPairingError("");
    try {
      const next = await startAccessUserPairing(selectedUser.id, { device_label: "native app" });
      setPairingCode(next);
      setPairingNow(Date.now() / 1000);
    } catch (err) {
      setPairingError(err instanceof Error ? err.message : String(err));
    } finally {
      setPairingBusy(false);
    }
  }, [canGeneratePairing, pairingBusy, selectedUser, startAccessUserPairing]);

  useEffect(() => {
    setPairingCode(null);
    setPairingError("");
  }, [selectedUserId]);

  const onDeleteUser = useCallback(async () => {
    if (!selectedUser || !canManage || busy) return;
    if (!window.confirm(t("core.ui.access.confirm_delete_user", { username: selectedUser.username }))) return;
    setBusy(true);
    setError("");
    try {
      await deleteAccessUser(selectedUser.id);
      setData((prev) => {
        if (!prev) return prev;
        const users = prev.users.filter((item) => item.id !== selectedUser.id);
        return { ...prev, users };
      });
      setSelectedUserId("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [busy, canManage, deleteAccessUser, selectedUser]);

  return (
    <div className="accessRoot screenRoot">
      <div className="settingsTopbar">
        <button className="iconButton" type="button" onClick={onClose} aria-label={t("core.actions.back")}>
          <i className="fa-solid fa-arrow-left" aria-hidden="true" />
        </button>
        <div className="settingsTopbarTitle">{t("core.ui.access.title")}</div>
        <div className="row accessTopbarActions">
          <button className="chipButton" type="button" onClick={() => void loadData()} disabled={loading || busy}>
            {t("core.actions.refresh")}
          </button>
          <button className="chipButton" type="button" onClick={() => void onLogout()}>
            {t("core.actions.sign_out")}
          </button>
        </div>
      </div>

      <div className="accessLayout">
        <div className="accessSidebar">
          <div className="modalSectionTitle">{t("core.ui.access.users.title")}</div>
          {loading ? <div className="settingsStatusMuted">{t("core.ui.loading")}</div> : null}
          {!loading && (!data || data.users.length === 0) ? <div className="settingsStatusMuted">{t("core.ui.access.users.none")}</div> : null}
          <div className="settingsSidebarList">
            {(data?.users || []).map((user) => {
              const selected = user.id === selectedUserId;
              return (
                <button
                  key={user.id}
                  type="button"
                  className={["settingsNavItem", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
                  onClick={() => setSelectedUserId(user.id)}
                >
                  <span className="settingsNavIcon">
                    <i className="fa-solid fa-user" aria-hidden="true" />
                  </span>
                  <span className="settingsNavText">
                    <span className="settingsNavTitleRow">
                      <span className="settingsNavTitle">{user.display_name || user.username}</span>
                      {user.id === currentUserId ? <span className="accessCurrentUserPill">{t("core.ui.access.current_user")}</span> : null}
                    </span>
                    <span className="settingsNavDesc">
                      {user.username} · {t("core.ui.access.users.sessions_count", { count: user.sessions })}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>

          {canManage ? (
            <div className="card">
              <div className="cardTitle">{t("core.ui.access.create_user.title")}</div>
              <input
                className="input"
                placeholder={t("core.ui.auth.field.username")}
                value={createUsername}
                onChange={(e) => setCreateUsername(e.target.value)}
              />
              <input
                className="input"
                placeholder={t("core.ui.auth.field.display_name")}
                value={createDisplayName}
                onChange={(e) => setCreateDisplayName(e.target.value)}
              />
              <div className="accessFixedSummary">
                <div className="accessSummaryItem">
                  <span className="accessSummaryLabel">{t("core.ui.access.fixed_access.role_label")}</span>
                  <strong>{formatRole(MVP_USER_ROLE)}</strong>
                </div>
                <div className="accessSummaryItem">
                  <span className="accessSummaryLabel">{t("core.ui.access.fixed_access.scope_label")}</span>
                  <strong>{t("core.ui.access.fixed_access.scope_all")}</strong>
                </div>
              </div>
              <input
                className="input"
                type="password"
                placeholder={t("core.ui.auth.field.password")}
                value={createPassword}
                onChange={(e) => setCreatePassword(e.target.value)}
              />
              <button className="primaryButton" type="button" onClick={() => void onCreateUser()} disabled={busy}>
                {t("core.ui.access.create_user.action")}
              </button>
            </div>
          ) : null}
        </div>

        <div className="accessMain">
          {!selectedUser ? (
            <div className="settingsStatusMuted">{t("core.ui.access.empty_state.select_user")}</div>
          ) : (
            <>
              <div className="settingsHeader">
                <div className="settingsHeaderTitle">{selectedUser.display_name || selectedUser.username}</div>
                <div className="settingsHeaderDesc">{selectedUser.username}</div>
              </div>

              <div className="settingsContent">
                <div className="card">
                  <div className="cardTitle">{t("core.ui.access.section.identity")}</div>
                  <label className="authLabel">{t("core.ui.auth.field.display_name")}</label>
                  <input className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} disabled={!canManage} />

                  <label className="authLabel">{t("core.ui.access.field.new_password_optional")}</label>
                  <input
                    className="input"
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    disabled={!canManage}
                  />

                  <label className="row">
                    <input type="checkbox" checked={isDisabled} onChange={(e) => setIsDisabled(e.target.checked)} disabled={!canManage} />
                    {t("core.ui.access.field.disabled")}
                  </label>

                  {canManage ? (
                    <div className="cardFooter">
                      <button className="primaryButton" type="button" onClick={() => void onSaveUser()} disabled={busy}>
                        {t("core.ui.access.action.save_user")}
                      </button>
                      <button className="dangerButton" type="button" onClick={() => void onDeleteUser()} disabled={busy}>
                        {t("core.ui.access.action.delete_user")}
                      </button>
                    </div>
                  ) : null}
                </div>

                {canGeneratePairing ? (
                  <div className="card accessPairingCard">
                    <div className="cardTitle">{t("core.ui.access.pairing.title")}</div>
                    <div className="cardBody">{t("core.ui.access.pairing.desc")}</div>

                    {pairingCode ? (
                      <div className={["accessPairingCode", pairingExpired ? "isExpired" : ""].filter(Boolean).join(" ")}>
                        <span className="srOnly">{t("core.ui.access.pairing.code_label")}</span>
                        {pairingCodeChunks.map((chunk, index) => (
                          <span className="accessPairingCodeChunk" key={`${chunk}-${index}`}>
                            {chunk}
                          </span>
                        ))}
                      </div>
                    ) : null}

                    {pairingCode ? (
                      <div className="accessPairingMeta" aria-live="polite">
                        <span>
                          {pairingExpired
                            ? t("core.ui.access.pairing.expired")
                            : t("core.ui.access.pairing.valid_for", { time: pairingRemainingLabel })}
                        </span>
                        <span>{t("core.ui.access.pairing.expires_at", { time: pairingExpiresAtLabel })}</span>
                      </div>
                    ) : null}

                    {pairingError ? <div className="errorText">{pairingError}</div> : null}

                    <div className="cardFooter">
                      <button className="primaryButton" type="button" onClick={() => void onStartPairing()} disabled={pairingBusy}>
                        {pairingBusy
                          ? t("core.ui.access.pairing.generating")
                          : pairingCode
                            ? t("core.ui.access.pairing.action.regenerate")
                            : t("core.ui.access.pairing.action.generate")}
                      </button>
                    </div>
                  </div>
                ) : null}

                <div className="card">
                  <div className="cardTitle">{t("core.ui.access.fixed_access.title")}</div>
                  <div className="accessFixedSummary">
                    <div className="accessSummaryItem">
                      <span className="accessSummaryLabel">{t("core.ui.access.fixed_access.role_label")}</span>
                      <strong>{formatRole(MVP_USER_ROLE)}</strong>
                    </div>
                    <div className="accessSummaryItem">
                      <span className="accessSummaryLabel">{t("core.ui.access.fixed_access.scope_label")}</span>
                      <strong>{t("core.ui.access.fixed_access.scope_all")}</strong>
                    </div>
                  </div>
                </div>
              </div>
            </>
          )}

          {error ? <div className="errorText">{error}</div> : null}
        </div>
      </div>
    </div>
  );
}
