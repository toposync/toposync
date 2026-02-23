import React, { useCallback, useEffect, useMemo, useState } from "react";

import type { AccessUsersPayload, AuthRole, AuthUser } from "../../util/api";
import { i18n } from "../../util/i18n";

const ROLE_OPTIONS: AuthRole[] = ["owner", "admin", "member", "service"];

type Translate = (key: string, params?: Record<string, unknown>, fallback?: string) => string;

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
  upsertAccessGrant: (
    userId: string,
    payload: {
      action: string;
      resource_type: string;
      include: string[];
      exclude: string[];
    },
  ) => Promise<AuthUser>;
  deleteAccessGrant: (userId: string, action: string, resourceType: string) => Promise<AuthUser>;
};

function parseSelectors(raw: string): string[] {
  return raw
    .split(/[,\n]/g)
    .map((item) => item.trim())
    .filter(Boolean);
}

function grantSummary(t: Translate, include: string[], exclude: string[]): string {
  if ((!include || include.length === 0) && (!exclude || exclude.length === 0)) {
    return t("core.ui.access.grants.summary.all");
  }
  if ((!include || include.length === 0) && exclude.length > 0) {
    return t("core.ui.access.grants.summary.all_except", { selectors: exclude.join(", ") });
  }
  if (include.length > 0 && (!exclude || exclude.length === 0)) {
    return t("core.ui.access.grants.summary.only", { selectors: include.join(", ") });
  }
  return t("core.ui.access.grants.summary.mixed", {
    include: include.join(", "),
    exclude: exclude.join(", "),
  });
}

export function AccessScreen({
  authUser,
  authMode,
  onClose,
  onLogout,
  listAccessUsers,
  createAccessUser,
  patchAccessUser,
  deleteAccessUser,
  upsertAccessGrant,
  deleteAccessGrant,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [data, setData] = useState<AccessUsersPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");
  const [selectedUserId, setSelectedUserId] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<AuthRole>("member");
  const [isDisabled, setIsDisabled] = useState(false);
  const [newPassword, setNewPassword] = useState("");

  const [createUsername, setCreateUsername] = useState("");
  const [createDisplayName, setCreateDisplayName] = useState("");
  const [createRole, setCreateRole] = useState<AuthRole>("member");
  const [createPassword, setCreatePassword] = useState("");

  const [grantResourceType, setGrantResourceType] = useState("core:extension");
  const [grantAction, setGrantAction] = useState("core:extension:use");
  const [grantIncludeRaw, setGrantIncludeRaw] = useState("");
  const [grantExcludeRaw, setGrantExcludeRaw] = useState("");

  const canManage = authMode === "bypass" || (authUser && (authUser.role === "owner" || authUser.role === "admin"));
  const formatRole = useCallback((value: AuthRole) => t(`core.ui.auth.role.${value}`, {}, value), [t]);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await listAccessUsers();
      setData(payload);
      setSelectedUserId((prev) => {
        if (prev && payload.users.some((item) => item.id === prev)) return prev;
        return payload.users[0]?.id ?? "";
      });
      const resourceTypes = Object.keys(payload.grants_catalog || {});
      if (resourceTypes.length > 0) {
        const firstResourceType = resourceTypes[0];
        setGrantResourceType(firstResourceType);
        const actions = payload.grants_catalog[firstResourceType] || [];
        setGrantAction(actions[0] || "");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setData({ users: [], grants_catalog: {} });
    } finally {
      setLoading(false);
    }
  }, [listAccessUsers]);

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
    setRole(selectedUser.role);
    setIsDisabled(Boolean(selectedUser.is_disabled));
    setNewPassword("");
  }, [selectedUser]);

  useEffect(() => {
    if (!data) return;
    const actions = data.grants_catalog[grantResourceType] || [];
    if (!actions.length) {
      setGrantAction("");
      return;
    }
    if (!actions.includes(grantAction)) {
      setGrantAction(actions[0]);
    }
  }, [data, grantAction, grantResourceType]);

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
        role,
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
  }, [busy, canManage, displayName, isDisabled, newPassword, patchAccessUser, role, selectedUser, updateUserInState]);

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
        role: createRole,
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
      setCreateRole("member");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [busy, canManage, createAccessUser, createDisplayName, createPassword, createRole, createUsername]);

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

  const onUpsertGrant = useCallback(async () => {
    if (!selectedUser || !canManage || busy) return;
    if (!grantAction.trim() || !grantResourceType.trim()) {
      setError(t("core.ui.access.error.select_action_resource"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const next = await upsertAccessGrant(selectedUser.id, {
        action: grantAction,
        resource_type: grantResourceType,
        include: parseSelectors(grantIncludeRaw),
        exclude: parseSelectors(grantExcludeRaw),
      });
      updateUserInState(next);
      setGrantIncludeRaw("");
      setGrantExcludeRaw("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [
    busy,
    canManage,
    grantAction,
    grantExcludeRaw,
    grantIncludeRaw,
    grantResourceType,
    selectedUser,
    updateUserInState,
    upsertAccessGrant,
  ]);

  const onDeleteGrant = useCallback(
    async (action: string, resourceType: string) => {
      if (!selectedUser || !canManage || busy) return;
      setBusy(true);
      setError("");
      try {
        const next = await deleteAccessGrant(selectedUser.id, action, resourceType);
        updateUserInState(next);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [busy, canManage, deleteAccessGrant, selectedUser, updateUserInState],
  );

  const resourceTypeOptions = Object.keys(data?.grants_catalog || {});
  const actionOptions = (data?.grants_catalog || {})[grantResourceType] || [];

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
                    </span>
                    <span className="settingsNavDesc">
                      {user.username} · {formatRole(user.role)} · {t("core.ui.access.users.sessions_count", { count: user.sessions })}
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
              <select className="input" value={createRole} onChange={(e) => setCreateRole(e.target.value as AuthRole)}>
                {ROLE_OPTIONS.map((item) => (
                  <option key={item} value={item}>
                    {formatRole(item)}
                  </option>
                ))}
              </select>
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

                  <label className="authLabel">{t("core.ui.access.field.role")}</label>
                  <select className="input" value={role} onChange={(e) => setRole(e.target.value as AuthRole)} disabled={!canManage}>
                    {ROLE_OPTIONS.map((item) => (
                      <option key={item} value={item}>
                        {formatRole(item)}
                      </option>
                    ))}
                  </select>

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

                <div className="card">
                  <div className="cardTitle">{t("core.ui.access.grants.title")}</div>
                  <div className="settingsStatusMuted">
                    {t("core.ui.access.grants.hint")}
                  </div>

                  <div className="sectionDivider" />

                  {selectedUser.grants.length === 0 ? <div className="settingsStatusMuted">{t("core.ui.access.grants.none")}</div> : null}
                  {selectedUser.grants.length > 0 ? (
                    <div className="accessGrantsList">
                      {selectedUser.grants.map((grant) => (
                        <div key={`${grant.action}:${grant.resource_type}`} className="card">
                          <div className="row accessGrantHeaderRow">
                            <div>
                              <strong>{grant.action}</strong>
                              <div className="settingsNavDesc">{grant.resource_type}</div>
                            </div>
                            {canManage ? (
                              <button
                                className="chipButton"
                                type="button"
                                onClick={() => void onDeleteGrant(grant.action, grant.resource_type)}
                                disabled={busy}
                              >
                                {t("core.ui.access.grants.action.remove")}
                              </button>
                            ) : null}
                          </div>
                          <div className="settingsStatusMuted">{grantSummary(t, grant.include, grant.exclude)}</div>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {canManage ? (
                    <>
                      <div className="sectionDivider" />
                      <div className="cardTitle">{t("core.ui.access.grants.form.title")}</div>

                      <label className="authLabel">{t("core.ui.access.grants.form.resource_type")}</label>
                      <select className="input" value={grantResourceType} onChange={(e) => setGrantResourceType(e.target.value)}>
                        {resourceTypeOptions.map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>

                      <label className="authLabel">{t("core.ui.access.grants.form.action")}</label>
                      <select className="input" value={grantAction} onChange={(e) => setGrantAction(e.target.value)}>
                        {actionOptions.map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>

                      <label className="authLabel">{t("core.ui.access.grants.form.include_selectors")}</label>
                      <textarea
                        className="input"
                        rows={3}
                        value={grantIncludeRaw}
                        onChange={(e) => setGrantIncludeRaw(e.target.value)}
                        placeholder={t("core.ui.access.grants.form.include_placeholder")}
                      />

                      <label className="authLabel">{t("core.ui.access.grants.form.exclude_selectors")}</label>
                      <textarea
                        className="input"
                        rows={3}
                        value={grantExcludeRaw}
                        onChange={(e) => setGrantExcludeRaw(e.target.value)}
                        placeholder={t("core.ui.access.grants.form.exclude_placeholder")}
                      />

                      <button className="primaryButton" type="button" onClick={() => void onUpsertGrant()} disabled={busy}>
                        {t("core.ui.access.grants.form.save")}
                      </button>
                    </>
                  ) : null}
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
