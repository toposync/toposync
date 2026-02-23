import React, { useCallback, useEffect, useMemo, useState } from "react";
import Select, { type GroupBase, type MultiValue } from "react-select";

import { getAccessOptions, type AccessOptionsPayload, type AccessUsersPayload, type AuthRole, type AuthUser } from "../../util/api";
import { i18n } from "../../util/i18n";
import { pipelinesReactSelectStyles } from "./pipelines/constants";

const ROLE_OPTIONS: AuthRole[] = ["owner", "admin", "member", "service"];
type SelectOption = { value: string; label: string };

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

type ResourceMeta = {
  title: string;
  desc: string;
  targetsLabel: string;
};

type ActionMeta = {
  title: string;
  desc: string;
};

function describeResourceType(t: Translate, resourceType: string): ResourceMeta {
  if (resourceType === "core:extension") {
    return {
      title: t("core.ui.access.resource.core_extension.title"),
      desc: t("core.ui.access.resource.core_extension.desc"),
      targetsLabel: t("core.ui.access.targets.extensions.label"),
    };
  }
  if (resourceType === "core:event") {
    return {
      title: t("core.ui.access.resource.core_event.title"),
      desc: t("core.ui.access.resource.core_event.desc"),
      targetsLabel: t("core.ui.access.targets.events.label"),
    };
  }
  if (resourceType === "core:area") {
    return {
      title: t("core.ui.access.resource.core_area.title"),
      desc: t("core.ui.access.resource.core_area.desc"),
      targetsLabel: t("core.ui.access.targets.areas.label"),
    };
  }
  return {
    title: resourceType,
    desc: t("core.ui.access.resource.unknown_desc"),
    targetsLabel: t("core.ui.access.targets.generic.label"),
  };
}

function describeAction(t: Translate, action: string): ActionMeta {
  if (action === "core:extension:use") {
    return { title: t("core.ui.access.action.core_extension_use.title"), desc: t("core.ui.access.action.core_extension_use.desc") };
  }
  if (action === "core:extension:settings:write") {
    return {
      title: t("core.ui.access.action.core_extension_settings_write.title"),
      desc: t("core.ui.access.action.core_extension_settings_write.desc"),
    };
  }
  if (action === "core:events:emit") {
    return { title: t("core.ui.access.action.core_events_emit.title"), desc: t("core.ui.access.action.core_events_emit.desc") };
  }
  if (action === "core:area:read") {
    return { title: t("core.ui.access.action.core_area_read.title"), desc: t("core.ui.access.action.core_area_read.desc") };
  }
  if (action === "core:area:control") {
    return { title: t("core.ui.access.action.core_area_control.title"), desc: t("core.ui.access.action.core_area_control.desc") };
  }
  if (action === "core:area:edit") {
    return { title: t("core.ui.access.action.core_area_edit.title"), desc: t("core.ui.access.action.core_area_edit.desc") };
  }
  return { title: action, desc: t("core.ui.access.action.unknown_desc") };
}

function clampList(items: string[], { limit }: { limit: number }): { shown: string; remaining: number } {
  const clean = items.filter(Boolean);
  if (clean.length <= limit) return { shown: clean.join(", "), remaining: 0 };
  const shown = clean.slice(0, limit).join(", ");
  return { shown, remaining: clean.length - limit };
}

function formatTargetsSummary(t: Translate, items: string[], labelById: Record<string, string>): string {
  const mapped = items.map((id) => labelById[id] || id);
  const { shown, remaining } = clampList(mapped, { limit: 4 });
  if (!shown) return "";
  if (remaining <= 0) return shown;
  return t("core.ui.access.scope.more_count", { shown, count: remaining }, `${shown} +${remaining}`);
}

function formatGrantScope(t: Translate, include: string[], exclude: string[], labelById: Record<string, string>): string {
  if ((!include || include.length === 0) && (!exclude || exclude.length === 0)) {
    return t("core.ui.access.grants.summary.all");
  }
  if ((!include || include.length === 0) && exclude.length > 0) {
    return t("core.ui.access.grants.summary.all_except", { selectors: formatTargetsSummary(t, exclude, labelById) });
  }
  if (include.length > 0 && (!exclude || exclude.length === 0)) {
    return t("core.ui.access.grants.summary.only", { selectors: formatTargetsSummary(t, include, labelById) });
  }
  return t("core.ui.access.grants.summary.mixed", {
    include: formatTargetsSummary(t, include, labelById),
    exclude: formatTargetsSummary(t, exclude, labelById),
  });
}

function formatEventLabel(t: Translate, pattern: string): string {
  if (pattern === "device.action_requested") return t("core.ui.access.targets.events.device_action_requested");
  if (pattern === "home_assistant.primary_action_requested") return t("core.ui.access.targets.events.ha_primary_action_requested");
  if (pattern === "home_assistant.service_call") return t("core.ui.access.targets.events.ha_service_call");
  return pattern;
}

function buildTargetOptions(
  t: Translate,
  accessOptions: AccessOptionsPayload | null,
  resourceType: string,
): Array<SelectOption | GroupBase<SelectOption>> {
  if (!accessOptions) return [];

  if (resourceType === "core:extension") {
    return accessOptions.extensions.map((ext) => ({ value: ext.id, label: ext.name || ext.id }));
  }

  if (resourceType === "core:event") {
    return accessOptions.event_patterns.map((pattern) => ({ value: pattern, label: formatEventLabel(t, pattern) }));
  }

  if (resourceType === "core:area") {
    return accessOptions.compositions.map((comp) => {
      const options: SelectOption[] = [
        { value: `${comp.id}.*`, label: t("core.ui.access.targets.areas.all_areas_in", { composition: comp.name }, `All areas (${comp.name})`) },
        ...comp.areas.map((area) => ({ value: `${comp.id}.${area.id}`, label: area.name || area.id })),
      ];
      return { label: comp.name || comp.id, options };
    });
  }

  return [];
}

function flattenTargetOptions(options: Array<SelectOption | GroupBase<SelectOption>>): SelectOption[] {
  const out: SelectOption[] = [];
  for (const item of options) {
    if (item && typeof item === "object" && "options" in item && Array.isArray((item as any).options)) {
      for (const opt of (item as GroupBase<SelectOption>).options) out.push(opt);
    } else if (item && typeof item === "object" && typeof (item as any).value === "string") {
      out.push(item as SelectOption);
    }
  }
  return out;
}

function buildLabelIndex(options: Array<SelectOption | GroupBase<SelectOption>>): Record<string, string> {
  const flat = flattenTargetOptions(options);
  const out: Record<string, string> = {};
  for (const opt of flat) {
    out[opt.value] = opt.label;
  }
  return out;
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
  const [accessOptions, setAccessOptions] = useState<AccessOptionsPayload | null>(null);
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
  const [grantScopeMode, setGrantScopeMode] = useState<"all" | "only">("all");
  const [grantInclude, setGrantInclude] = useState<string[]>([]);
  const [grantExclude, setGrantExclude] = useState<string[]>([]);

  const canManage = authMode === "bypass" || (authUser && (authUser.role === "owner" || authUser.role === "admin"));
  const formatRole = useCallback((value: AuthRole) => t(`core.ui.auth.role.${value}`, {}, value), [t]);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [payload, opts] = await Promise.all([listAccessUsers(), getAccessOptions()]);
      setData(payload);
      setAccessOptions(opts);
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
      setAccessOptions({ extensions: [], compositions: [], event_patterns: [] });
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

  const onEditGrant = useCallback(
    (grant: AuthUser["grants"][number]) => {
      setGrantResourceType(grant.resource_type);
      setGrantAction(grant.action);
      setGrantScopeMode(grant.include.length === 0 ? "all" : "only");
      setGrantInclude(grant.include);
      setGrantExclude(grant.exclude);
    },
    [],
  );

  const onChangeGrantResourceType = useCallback(
    (next: string) => {
      setGrantResourceType(next);
      const actions = (data?.grants_catalog || {})[next] || [];
      setGrantAction(actions[0] || "");
      setGrantScopeMode("all");
      setGrantInclude([]);
      setGrantExclude([]);
    },
    [data?.grants_catalog],
  );

  const onChangeGrantAction = useCallback((next: string) => {
    setGrantAction(next);
    setGrantScopeMode("all");
    setGrantInclude([]);
    setGrantExclude([]);
  }, []);

  const onUpsertGrant = useCallback(async () => {
    if (!selectedUser || !canManage || busy) return;
    if (!grantAction.trim() || !grantResourceType.trim()) {
      setError(t("core.ui.access.error.select_action_resource"));
      return;
    }
    if (grantScopeMode === "only" && grantInclude.length === 0) {
      setError(t("core.ui.access.error.select_scope_items"));
      return;
    }
    setBusy(true);
    setError("");
    try {
      const include = grantScopeMode === "all" ? [] : grantInclude;
      const next = await upsertAccessGrant(selectedUser.id, {
        action: grantAction,
        resource_type: grantResourceType,
        include,
        exclude: grantExclude,
      });
      updateUserInState(next);
      setGrantScopeMode("all");
      setGrantInclude([]);
      setGrantExclude([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [
    busy,
    canManage,
    grantAction,
    grantExclude,
    grantInclude,
    grantResourceType,
    grantScopeMode,
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

  const grantResourceMeta = useMemo(() => describeResourceType(t, grantResourceType), [grantResourceType, t]);
  const grantActionMeta = useMemo(() => describeAction(t, grantAction), [grantAction, t]);
  const targetOptions = useMemo(
    () => buildTargetOptions(t, accessOptions, grantResourceType),
    [accessOptions, grantResourceType, t],
  );
  const grantLabelsByResourceType = useMemo<Record<string, Record<string, string>>>(() => {
    return {
      "core:extension": buildLabelIndex(buildTargetOptions(t, accessOptions, "core:extension")),
      "core:event": buildLabelIndex(buildTargetOptions(t, accessOptions, "core:event")),
      "core:area": buildLabelIndex(buildTargetOptions(t, accessOptions, "core:area")),
    };
  }, [accessOptions, t]);
  const targetOptionsFlat = useMemo(() => flattenTargetOptions(targetOptions), [targetOptions]);

  const includeValue = useMemo(() => {
    return grantInclude.map((id) => targetOptionsFlat.find((opt) => opt.value === id) ?? { value: id, label: id });
  }, [grantInclude, targetOptionsFlat]);

  const excludeValue = useMemo(() => {
    return grantExclude.map((id) => targetOptionsFlat.find((opt) => opt.value === id) ?? { value: id, label: id });
  }, [grantExclude, targetOptionsFlat]);

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
                  <div className="settingsStatusMuted">{t("core.ui.access.grants.hint")}</div>

                  <div className="sectionDivider" />

                  {selectedUser.grants.length === 0 ? <div className="settingsStatusMuted">{t("core.ui.access.grants.none")}</div> : null}
                  {selectedUser.grants.length > 0 ? (
                    <div className="accessGrantsList">
                      {selectedUser.grants.map((grant) => {
                        const resourceMeta = describeResourceType(t, grant.resource_type);
                        const actionMeta = describeAction(t, grant.action);
                        const labels = grantLabelsByResourceType[grant.resource_type] || {};
                        return (
                          <div key={`${grant.action}:${grant.resource_type}`} className="card">
                            <div className="row accessGrantHeaderRow">
                              <div>
                                <strong>{actionMeta.title}</strong>
                                <div className="settingsNavDesc">{resourceMeta.title}</div>
                              </div>
                              {canManage ? (
                                <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
                                  <button className="chipButton" type="button" onClick={() => onEditGrant(grant)} disabled={busy}>
                                    {t("core.actions.edit")}
                                  </button>
                                  <button
                                    className="chipButton"
                                    type="button"
                                    onClick={() => void onDeleteGrant(grant.action, grant.resource_type)}
                                    disabled={busy}
                                  >
                                    {t("core.ui.access.grants.action.remove")}
                                  </button>
                                </div>
                              ) : null}
                            </div>
                            <div className="settingsStatusMuted">{formatGrantScope(t, grant.include, grant.exclude, labels)}</div>
                          </div>
                        );
                      })}
                    </div>
                  ) : null}

                  {canManage ? (
                    <>
                      <div className="sectionDivider" />
                      <div className="cardTitle">{t("core.ui.access.grants.form.title")}</div>

                      <label className="authLabel">{t("core.ui.access.grants.form.resource_type")}</label>
                      <select className="input" value={grantResourceType} onChange={(e) => onChangeGrantResourceType(e.target.value)}>
                        {resourceTypeOptions.map((item) => (
                          <option key={item} value={item}>
                            {describeResourceType(t, item).title}
                          </option>
                        ))}
                      </select>
                      <div className="settingsStatusMuted">{grantResourceMeta.desc}</div>

                      <label className="authLabel">{t("core.ui.access.grants.form.action")}</label>
                      <select className="input" value={grantAction} onChange={(e) => onChangeGrantAction(e.target.value)}>
                        {actionOptions.map((item) => (
                          <option key={item} value={item}>
                            {describeAction(t, item).title}
                          </option>
                        ))}
                      </select>
                      {grantAction ? <div className="settingsStatusMuted">{grantActionMeta.desc}</div> : null}

                      <div className="sectionDivider" />
                      <div className="modalSectionTitle">{t("core.ui.access.grants.form.scope_title")}</div>
                      <div className="choiceList">
                        <div
                          className={["choiceItem", grantScopeMode === "all" ? "isSelected" : ""].filter(Boolean).join(" ")}
                          role="button"
                          tabIndex={0}
                          onClick={() => setGrantScopeMode("all")}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") setGrantScopeMode("all");
                          }}
                        >
                          <div className="choiceTitle">{t("core.ui.access.grants.form.scope_all_title")}</div>
                          <div className="choiceDesc">{t("core.ui.access.grants.form.scope_all_desc")}</div>
                        </div>
                        <div
                          className={["choiceItem", grantScopeMode === "only" ? "isSelected" : ""].filter(Boolean).join(" ")}
                          role="button"
                          tabIndex={0}
                          onClick={() => setGrantScopeMode("only")}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") setGrantScopeMode("only");
                          }}
                        >
                          <div className="choiceTitle">{t("core.ui.access.grants.form.scope_only_title")}</div>
                          <div className="choiceDesc">{t("core.ui.access.grants.form.scope_only_desc")}</div>
                        </div>
                      </div>

                      {grantScopeMode === "only" ? (
                        <>
                          <div className="sectionDivider" />
                          <label className="authLabel">{t("core.ui.access.grants.form.include_items", { label: grantResourceMeta.targetsLabel })}</label>
                          <Select<SelectOption, true, GroupBase<SelectOption>>
                            isMulti
                            isDisabled={busy}
                            styles={pipelinesReactSelectStyles as any}
                            options={targetOptions as any}
                            value={includeValue}
                            placeholder={t("core.ui.access.targets.generic.placeholder")}
                            onChange={(items: MultiValue<SelectOption>) => setGrantInclude(items.map((opt) => opt.value))}
                            noOptionsMessage={() => t("core.ui.access.targets.generic.none")}
                          />
                        </>
                      ) : null}

                      <div className="sectionDivider" />
                      <label className="authLabel">{t("core.ui.access.grants.form.exclude_items", { label: grantResourceMeta.targetsLabel })}</label>
                      <Select<SelectOption, true, GroupBase<SelectOption>>
                        isMulti
                        isDisabled={busy}
                        styles={pipelinesReactSelectStyles as any}
                        options={targetOptions as any}
                        value={excludeValue}
                        placeholder={t("core.ui.access.targets.generic.exclude_placeholder")}
                        onChange={(items: MultiValue<SelectOption>) => setGrantExclude(items.map((opt) => opt.value))}
                        noOptionsMessage={() => t("core.ui.access.targets.generic.none")}
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
