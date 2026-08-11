import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { HostApi, HostI18n, SettingsPanel } from "@toposync/plugin-api";

import {
  AttentionApiError,
  commandAttentionProfile,
  createAttentionProfile,
  deleteAttentionProfile,
  fetchAttentionCatalog,
  fetchAttentionDecisions,
  fetchAttentionProfiles,
  fetchAttentionStatus,
  updateAttentionProfile,
  validateAttentionProfile,
  validateStoredAttentionProfile,
} from "../api";
import type {
  AttentionCameraCatalogItem,
  AttentionCatalogResponse,
  AttentionCommand,
  AttentionDecision,
  AttentionExecutionMode,
  AttentionProfile,
  AttentionReadinessIssue,
  AttentionRuntimeState,
  AttentionSession,
  AttentionStatusResponse,
  AttentionValidationResponse,
} from "../types";
import { PtzAttentionWizard } from "./PtzAttentionWizard";

type Translate = ReturnType<HostI18n["useI18n"]>["t"];
type Tone = "success" | "warning" | "danger" | "info" | "neutral";

export function createPtzAttentionSettingsPanel(): SettingsPanel {
  return {
    id: "com.toposync.ptz_attention",
    icon: "crosshairs",
    name: { key: "ext.ptz_attention.settings.name", fallback: "PTZ Attention" },
    description: { key: "ext.ptz_attention.settings.desc" },
    render: ({ i18n, api }) => <PtzAttentionSettingsPanelContent i18n={i18n} api={api} />,
  };
}

function PtzAttentionSettingsPanelContent({ i18n, api }: { i18n: HostI18n; api: HostApi }): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const [profiles, setProfiles] = useState<AttentionProfile[]>([]);
  const [catalog, setCatalog] = useState<AttentionCatalogResponse | null>(null);
  const [status, setStatus] = useState<AttentionStatusResponse | null>(null);
  const [validation, setValidation] = useState<AttentionValidationResponse | null>(null);
  const [validationLoading, setValidationLoading] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<AttentionDecision[]>([]);
  const [decisionsLoading, setDecisionsLoading] = useState(false);
  const [decisionsError, setDecisionsError] = useState<string | null>(null);
  const [decisionsCursor, setDecisionsCursor] = useState<number | null>(null);
  const [decisionsPaging, setDecisionsPaging] = useState(false);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [permissionDenied, setPermissionDenied] = useState(false);
  const [partialErrors, setPartialErrors] = useState<string[]>([]);
  const [commandBusy, setCommandBusy] = useState(false);
  const [profileDeleting, setProfileDeleting] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [editingProfile, setEditingProfile] = useState<AttentionProfile | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const selectedProfileIdRef = useRef("");
  const decisionsPagingControllerRef = useRef<AbortController | null>(null);

  const loadCore = useCallback(async (signal?: AbortSignal, showRefreshing = false): Promise<void> => {
    if (showRefreshing) setRefreshing(true);
    const results = await Promise.allSettled([
      fetchAttentionProfiles(api, signal),
      fetchAttentionCatalog(api, signal),
      fetchAttentionStatus(api, signal),
    ]);
    if (signal?.aborted) return;
    const errors: string[] = [];
    let denied = false;
    const collectError = (reason: unknown) => {
      if (reason instanceof AttentionApiError && (reason.status === 401 || reason.status === 403)) denied = true;
      errors.push(reason instanceof Error ? reason.message : String(reason));
    };
    if (results[0].status === "fulfilled") setProfiles(normalizeProfiles(results[0].value)); else collectError(results[0].reason);
    if (results[1].status === "fulfilled") {
      setCatalog(results[1].value);
      errors.push(...results[1].value.partial_errors.map((issue) => issue.message || issue.code));
    } else {
      setCatalog(null);
      collectError(results[1].reason);
    }
    if (results[2].status === "fulfilled") {
      setStatus(normalizeStatus(results[2].value));
    } else {
      setStatus(null);
      collectError(results[2].reason);
    }
    setPermissionDenied(denied && results.every((result) => result.status === "rejected"));
    setPartialErrors(Array.from(new Set(errors)));
    setInitialLoading(false);
    setRefreshing(false);
  }, [api]);

  const validateProfile = useCallback(
    (profile: AttentionProfile, signal?: AbortSignal) => validateAttentionProfile(api, profile, signal),
    [api],
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadCore(controller.signal);
    return () => controller.abort();
  }, [loadCore]);

  useEffect(() => {
    if (permissionDenied) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void fetchAttentionStatus(api, controller.signal)
        .then((payload) => setStatus(normalizeStatus(payload)))
        .catch(() => undefined);
    }, 2500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [api, permissionDenied]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (selectedProfileId && profiles.some((profile) => profile.id === selectedProfileId)) return;
    const profileId = profiles[0]?.id ?? "";
    selectedProfileIdRef.current = profileId;
    setSelectedProfileId(profileId);
  }, [profiles, selectedProfileId]);

  useEffect(() => {
    if (!selectedProfileId || permissionDenied) {
      setDecisions([]);
      setDecisionsLoading(false);
      setDecisionsError(null);
      setDecisionsCursor(null);
      setDecisionsPaging(false);
      return;
    }
    setDecisions([]);
    setDecisionsLoading(true);
    setDecisionsError(null);
    setDecisionsCursor(null);
    setDecisionsPaging(false);
    decisionsPagingControllerRef.current?.abort();
    decisionsPagingControllerRef.current = null;
    let cancelled = false;
    const controller = new AbortController();
    const load = (initial = false) => {
      void fetchAttentionDecisions(api, selectedProfileId, controller.signal)
        .then((payload) => {
          if (!cancelled) {
            const incoming = normalizeDecisions(payload.decisions);
            setDecisions((current) => initial ? incoming : mergeDecisions(incoming, current));
            if (initial) setDecisionsCursor(normalizeCursor(payload.next_cursor));
            setDecisionsError(null);
          }
        })
        .catch((reason) => {
          if (!cancelled && !controller.signal.aborted) {
            setDecisionsError(reason instanceof Error ? reason.message : String(reason));
          }
        })
        .finally(() => {
          if (initial && !cancelled) setDecisionsLoading(false);
        });
    };
    load(true);
    const timer = window.setInterval(() => load(false), 3500);
    return () => {
      cancelled = true;
      controller.abort();
      decisionsPagingControllerRef.current?.abort();
      decisionsPagingControllerRef.current = null;
      window.clearInterval(timer);
    };
  }, [api, permissionDenied, selectedProfileId]);

  useEffect(() => {
    if (!selectedProfileId || permissionDenied) {
      setValidation(null);
      setValidationLoading(false);
      setValidationError(null);
      return;
    }
    const controller = new AbortController();
    setValidation(null);
    setValidationLoading(true);
    setValidationError(null);
    void validateStoredAttentionProfile(api, selectedProfileId, controller.signal)
      .then((payload) => {
        if (!controller.signal.aborted) {
          setValidation(payload);
          setValidationError(null);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setValidationError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setValidationLoading(false);
      });
    return () => controller.abort();
  }, [api, permissionDenied, profiles, selectedProfileId]);

  const selectedProfile = profiles.find((profile) => profile.id === selectedProfileId) ?? null;
  const session = selectedProfile
    ? status?.devices.find((item) => item.profile_id === selectedProfile.id || item.ptz_device_id === selectedProfile.ptz_device_id) ?? null
    : null;
  const camera = selectedProfile ? catalog?.cameras.find((item) => item.id === selectedProfile.camera_id) ?? null : null;
  const globalConfigure = catalog?.permissions.configure === true;
  const globalControl = catalog?.permissions.control === true;
  const canCreateProfile = Boolean(
    globalConfigure || catalog?.cameras.some((item) => item.permissions.configure),
  );
  const canConfigure = Boolean(globalConfigure || camera?.permissions.configure);
  const canControl = Boolean(globalControl || camera?.permissions.control);
  const bindingReady = Boolean(selectedProfile && catalog?.bindings.some(
    (binding) =>
      binding.profile_id === selectedProfile.id &&
      binding.enabled &&
      selectedProfile.event_policies.some(
        (policy) =>
          policy.enabled &&
          (binding.event_type ? binding.event_type === policy.event_type : Boolean(binding.event_type_field)),
      ),
  ));
  const sharedObserver = Boolean(selectedProfile && catalog?.bindings.some(
    (binding) =>
      binding.profile_id === selectedProfile.id &&
      binding.enabled &&
      binding.observer_camera_id === selectedProfile.camera_id,
  ));
  const staleAge = status?.generated_at ? Math.max(0, now - status.generated_at) : 0;
  const statusStale = Boolean(status?.generated_at && staleAge > 10);

  const profileIssues = useMemo(() => validation?.issues ?? [], [validation?.issues]);
  const sessionActive = !status || statusStale || !session || Boolean(
    session.lease_active || !["IDLE", "MANUAL_OVERRIDE", "FAULT"].includes(session.state),
  );
  const canDeleteSelected = Boolean(
    selectedProfile &&
    canConfigure &&
    !sessionActive &&
    (selectedProfile.mode === "paused" || selectedProfile.mode === "disabled"),
  );
  const liveResumeBlocked = Boolean(
    selectedProfile?.mode === "paused" &&
    selectedProfile.resume_mode === "live_preset" &&
    (
      validationLoading ||
      validationError ||
      validation?.ok !== true ||
      profileIssues.some((issue) => issue.blocking || issue.severity === "error")
    ),
  );

  async function runCommand(command: AttentionCommand): Promise<void> {
    if (
      !canControl ||
      !selectedProfile ||
      !session ||
      commandBusy ||
      statusStale ||
      !status ||
      (command === "resume" && liveResumeBlocked) ||
      (command === "return-home" && session.state === "MANUAL_OVERRIDE")
    ) return;
    setCommandBusy(true);
    setSuccess(null);
    try {
      if (command === "resume" && selectedProfile.resume_mode === "live_preset") {
        const latestValidation = await validateStoredAttentionProfile(api, selectedProfile.id);
        setValidation(latestValidation);
        setValidationError(null);
        const blocked = !latestValidation.ok || latestValidation.issues.some(
          (issue) => issue.blocking || issue.severity === "error",
        );
        if (blocked) {
          throw new Error(t(
            "ext.ptz_attention.profile.resume.blocked",
            {},
            "Live movement cannot resume until the readiness check succeeds.",
          ));
        }
      }
      await commandAttentionProfile(api, selectedProfile.id, command);
      setSuccess(t("ext.ptz_attention.success.command", {}, "Command accepted. Waiting for runtime confirmation."));
      await loadCore(undefined, false);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      if (command === "resume" && selectedProfile.resume_mode === "live_preset") {
        setValidationError(message);
      }
      setPartialErrors([message]);
    } finally {
      setCommandBusy(false);
    }
  }

  async function saveProfile(profile: AttentionProfile): Promise<void> {
    const targetCamera = catalog?.cameras.find((item) => item.id === profile.camera_id) ?? null;
    if (!(globalConfigure || targetCamera?.permissions.configure)) {
      throw new Error(t("ext.ptz_attention.permission.configure", {}, "Profile configuration permission is required."));
    }
    const editing = editingProfile !== null;
    const saved = editing ? await updateAttentionProfile(api, profile) : await createAttentionProfile(api, profile);
    await loadCore(undefined, false);
    setDecisions([]);
    setDecisionsCursor(null);
    selectedProfileIdRef.current = saved.id;
    setSelectedProfileId(saved.id);
    setWizardOpen(false);
    setEditingProfile(null);
    setSuccess(
      editing
        ? t("ext.ptz_attention.success.updated", {}, "Profile updated.")
        : t("ext.ptz_attention.success.created", { mode: modeLabel(saved.mode, t) }, "Profile created."),
    );
  }

  async function deleteSelectedProfile(): Promise<void> {
    if (!selectedProfile || !canDeleteSelected || profileDeleting) return;
    const confirmed = window.confirm(t(
      "ext.ptz_attention.profile.delete.confirm",
      { name: selectedProfile.name },
      `Delete profile '${selectedProfile.name}'? This cannot be undone.`,
    ));
    if (!confirmed) return;
    setProfileDeleting(true);
    setSuccess(null);
    try {
      await deleteAttentionProfile(api, selectedProfile.id);
      selectedProfileIdRef.current = "";
      setSelectedProfileId("");
      await loadCore(undefined, false);
      setSuccess(t("ext.ptz_attention.success.deleted", {}, "Profile deleted."));
    } catch (reason) {
      setPartialErrors([reason instanceof Error ? reason.message : String(reason)]);
    } finally {
      setProfileDeleting(false);
    }
  }

  async function loadOlderDecisions(): Promise<void> {
    if (!selectedProfile || decisionsCursor === null || decisionsPaging) return;
    const profileId = selectedProfile.id;
    const controller = new AbortController();
    decisionsPagingControllerRef.current?.abort();
    decisionsPagingControllerRef.current = controller;
    setDecisionsPaging(true);
    try {
      const payload = await fetchAttentionDecisions(api, profileId, controller.signal, decisionsCursor);
      if (controller.signal.aborted || selectedProfileIdRef.current !== profileId) return;
      setDecisions((current) => mergeDecisions(current, normalizeDecisions(payload.decisions)));
      setDecisionsCursor(normalizeCursor(payload.next_cursor));
      setDecisionsError(null);
    } catch (reason) {
      if (!controller.signal.aborted && selectedProfileIdRef.current === profileId) {
        setDecisionsError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (decisionsPagingControllerRef.current === controller) {
        decisionsPagingControllerRef.current = null;
      }
      if (selectedProfileIdRef.current === profileId) setDecisionsPaging(false);
    }
  }

  const closeWizard = useCallback(() => {
    setWizardOpen(false);
    setEditingProfile(null);
  }, []);

  if (initialLoading) {
    return (
      <div className="settingsPanel ptzAttentionPanel">
        <div className="ptzAttentionCard"><div className="ptzAttentionEmpty" role="status">{t("ext.ptz_attention.loading", {}, "Loading PTZ Attention...")}</div></div>
      </div>
    );
  }

  if (permissionDenied) {
    return (
      <div className="settingsPanel ptzAttentionPanel">
        <EmptyState
          icon="fa-lock"
          title={t("ext.ptz_attention.permission.title", {}, "Camera access required")}
          description={t("ext.ptz_attention.permission.desc", {}, "This user cannot inspect or control PTZ Attention.")}
        />
      </div>
    );
  }

  return (
    <div className="settingsPanel ptzAttentionPanel">
      <header className="ptzAttentionHeader">
        <div>
          <div className="settingsTitle">{t("ext.ptz_attention.settings.title", {}, "PTZ Attention")}</div>
          <div className="settingsDescription">{t("ext.ptz_attention.settings.subtitle", {}, "Semantic PTZ focus and safe return.")}</div>
        </div>
        <div className="ptzAttentionHeaderActions">
          <button className="chipButton" type="button" disabled={refreshing || commandBusy || profileDeleting} onClick={() => void loadCore(undefined, true)}>
            <i className="fa-solid fa-rotate" aria-hidden="true" />
            <span>{t("ext.ptz_attention.action.refresh", {}, "Refresh")}</span>
          </button>
          <button className="primaryButton" type="button" disabled={!canCreateProfile || commandBusy || profileDeleting} onClick={() => { setEditingProfile(null); setWizardOpen(true); }}>
            <i className="fa-solid fa-plus" aria-hidden="true" />
            <span>{t("ext.ptz_attention.action.create", {}, "New profile")}</span>
          </button>
        </div>
      </header>

      {catalog && (!globalConfigure || !globalControl) ? (
        <div className="ptzAttentionNotice" data-tone="info" role="status" style={{ marginBottom: 12 }}>
          {t(
            "ext.ptz_attention.permission.limited",
            {},
            "Current permissions allow inspection, but some configuration or control actions are unavailable.",
          )}
        </div>
      ) : null}
      {partialErrors.length ? (
        <div className="ptzAttentionNotice" data-tone="warning" role="status" style={{ marginBottom: 12 }}>
          <strong>{t("ext.ptz_attention.loading.partial", {}, "Some operational data is unavailable.")}</strong>
          <div className="ptzAttentionSubtle" style={{ marginTop: 4 }}>{partialErrors[0]}</div>
        </div>
      ) : null}
      {statusStale ? (
        <div className="ptzAttentionNotice" data-tone="warning" role="alert" style={{ marginBottom: 12 }}>
          <strong>{t("ext.ptz_attention.stale.title", {}, "Runtime status is stale")}</strong>
          <div className="ptzAttentionMuted">{t("ext.ptz_attention.stale.desc", { seconds: Math.round(staleAge) }, "Last update is stale.")}</div>
        </div>
      ) : null}
      {success ? <div className="ptzAttentionNotice" data-tone="success" role="status" style={{ marginBottom: 12 }}>{success}</div> : null}

      {profiles.length === 0 ? (
        <EmptyState
          icon="fa-crosshairs"
          title={t("ext.ptz_attention.empty.title", {}, "No attention profile yet")}
          description={t("ext.ptz_attention.empty.desc", {}, "Start in shadow mode without moving hardware.")}
          actionLabel={canCreateProfile ? t("ext.ptz_attention.empty.action", {}, "Create first profile") : undefined}
          onAction={canCreateProfile ? () => { setEditingProfile(null); setWizardOpen(true); } : undefined}
        />
      ) : (
        <div className="ptzAttentionLayout">
          <main className="ptzAttentionMain">
            {selectedProfile ? (
              <SessionCockpit
                t={t}
                locale={locale}
                profile={selectedProfile}
                session={session}
                camera={camera}
                currentViewId={currentViewId(session)}
                bindingReady={bindingReady}
                sharedObserver={sharedObserver}
                stale={statusStale || !status || !session}
                busy={commandBusy || profileDeleting}
                canConfigure={canConfigure}
                canControl={canControl}
                editBlocked={sessionActive}
                deleteAllowed={canDeleteSelected}
                liveResumeBlocked={liveResumeBlocked}
                onCommand={(command) => void runCommand(command)}
                onEdit={() => { setEditingProfile(selectedProfile); setWizardOpen(true); }}
                onDelete={() => void deleteSelectedProfile()}
              />
            ) : null}
            <DecisionReview
              t={t}
              locale={locale}
              decisions={decisions}
              camera={camera}
              loading={decisionsLoading}
              error={decisionsError}
              hasMore={decisionsCursor !== null}
              paging={decisionsPaging}
              onLoadMore={() => void loadOlderDecisions()}
            />
            <DiagnosticsCard
              t={t}
              issues={profileIssues}
              validation={validation}
              loading={validationLoading}
              error={validationError}
              session={session}
              onRetry={() => void runCommand("return-home")}
              disabled={statusStale || !status || !session || commandBusy || !canControl}
            />
          </main>
          <aside className="ptzAttentionRail">
            <div className="ptzAttentionCard">
              <div className="ptzAttentionCardBody">
                <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.profiles.title", {}, "Profiles")}</h3>
                <div className="ptzAttentionProfileList">
                  {profiles.map((profile) => {
                    const profileSession = status?.devices.find((item) => item.profile_id === profile.id || item.ptz_device_id === profile.ptz_device_id) ?? null;
                    return (
                      <button
                        className="ptzAttentionProfileButton"
                        type="button"
                        data-selected={profile.id === selectedProfileId}
                        aria-current={profile.id === selectedProfileId ? "true" : undefined}
                        disabled={commandBusy || profileDeleting || decisionsPaging}
                        key={profile.id}
                        onClick={() => {
                          if (profile.id !== selectedProfileId) {
                            decisionsPagingControllerRef.current?.abort();
                            decisionsPagingControllerRef.current = null;
                            setDecisions([]);
                            setDecisionsCursor(null);
                            setDecisionsError(null);
                            setDecisionsLoading(true);
                            setValidation(null);
                            setValidationError(null);
                            setValidationLoading(true);
                          }
                          selectedProfileIdRef.current = profile.id;
                          setSelectedProfileId(profile.id);
                          setSuccess(null);
                        }}
                      >
                        <div className="ptzAttentionProfileName">{profile.name}</div>
                        <div className="ptzAttentionBadgeRow" style={{ marginTop: 7 }}>
                          <StatusBadge tone={modeTone(profile.mode)}>{modeLabel(profile.mode, t)}</StatusBadge>
                          <StatusBadge tone={profileSession ? stateTone(profileSession.state) : "neutral"}>
                            {profileSession
                              ? stateLabel(profileSession.state, t)
                              : t("ext.ptz_attention.session.unavailable", {}, "Runtime unavailable")}
                          </StatusBadge>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          </aside>
        </div>
      )}

      <PtzAttentionWizard
        open={wizardOpen}
        i18n={i18n}
        catalog={catalog}
        existingProfiles={profiles}
        initialProfile={editingProfile}
        onClose={closeWizard}
        onValidate={validateProfile}
        onSave={saveProfile}
      />
    </div>
  );
}

function SessionCockpit({
  t,
  locale,
  profile,
  session,
  camera,
  currentViewId,
  bindingReady,
  sharedObserver,
  stale,
  busy,
  canConfigure,
  canControl,
  editBlocked,
  deleteAllowed,
  liveResumeBlocked,
  onCommand,
  onEdit,
  onDelete,
}: {
  t: Translate;
  locale: string;
  profile: AttentionProfile;
  session: AttentionSession | null;
  camera: AttentionCameraCatalogItem | null;
  currentViewId: string;
  bindingReady: boolean;
  sharedObserver: boolean;
  stale: boolean;
  busy: boolean;
  canConfigure: boolean;
  canControl: boolean;
  editBlocked: boolean;
  deleteAllowed: boolean;
  liveResumeBlocked: boolean;
  onCommand: (command: AttentionCommand) => void;
  onEdit: () => void;
  onDelete: () => void;
}): React.ReactElement {
  const state = session?.state;
  const stateIndex = state ? timelineIndex(state) : -1;
  const activeEvent = session?.active_event_key || session?.candidate_event_key || "-";
  const activeView = camera?.views.find((view) => view.id === currentViewId) ?? null;
  const controlsDisabled = stale || busy || !canControl;
  return (
    <div className="ptzAttentionCard">
      <div className="ptzAttentionCardBody">
        <div className="ptzAttentionHero">
          <div>
            <div className="ptzAttentionSubtle">{t("ext.ptz_attention.session.title", {}, "Current session")}</div>
            <h2 className="ptzAttentionSessionTitle">
              {state ? stateLabel(state, t) : t("ext.ptz_attention.session.unavailable", {}, "Runtime unavailable")}
            </h2>
            <div className="ptzAttentionBadgeRow" style={{ marginTop: 10 }}>
              <StatusBadge tone={modeTone(profile.mode)}>{modeLabel(profile.mode, t)}</StatusBadge>
              <StatusBadge tone={state ? stateTone(state) : "neutral"}>{profile.name}</StatusBadge>
              {session?.movements_last_minute ? <StatusBadge tone="warning">{session.movements_last_minute}/min</StatusBadge> : null}
            </div>
          </div>
          <button className="chipButton" type="button" disabled={!canConfigure || editBlocked || busy} onClick={onEdit}>
            <i className="fa-solid fa-pen" aria-hidden="true" />
            <span>{t("ext.ptz_attention.action.edit", {}, "Edit profile")}</span>
          </button>
        </div>

        <div className="ptzAttentionTimeline" aria-label={t("ext.ptz_attention.session.title", {}, "Current session") }>
          {[
            t("ext.ptz_attention.timeline.home", {}, "Home"),
            t("ext.ptz_attention.timeline.acquiring", {}, "Acquiring"),
            t("ext.ptz_attention.timeline.focused", {}, "Focused"),
            t("ext.ptz_attention.timeline.returning", {}, "Return"),
          ].map((label, index) => (
            <div
              className="ptzAttentionStage"
              data-active={stateIndex === index}
              data-complete={stateIndex > index}
              aria-current={stateIndex === index ? "step" : undefined}
              key={label}
            >
              <span className="ptzAttentionStageNumber">{index + 1}</span>
              <div className="ptzAttentionStageLabel">{label}</div>
            </div>
          ))}
        </div>

        {profile.mode === "shadow" ? <SessionNotice tone="info">{t("ext.ptz_attention.session.shadow_notice", {}, "Shadow review active.")}</SessionNotice> : null}
        {!bindingReady ? <SessionNotice tone="warning">{t("ext.ptz_attention.session.binding_notice", {}, "No enabled pipeline binding is ready for this profile.")}</SessionNotice> : null}
        {sharedObserver ? <SessionNotice tone="warning">{t("ext.ptz_attention.session.shared_observer_notice", {}, "The event observer and PTZ actuator are the same camera; focus movement changes the observed scene.")}</SessionNotice> : null}
        {state === "MANUAL_OVERRIDE" ? <SessionNotice tone="warning">{t("ext.ptz_attention.session.manual_notice", {}, "Manual control owns the camera.")}</SessionNotice> : null}
        {state === "FAULT" ? <SessionNotice tone="danger">{t("ext.ptz_attention.session.fault_notice", {}, "Automation stopped after a fault.")}</SessionNotice> : null}
        {canConfigure && editBlocked ? <SessionNotice tone="warning">{t("ext.ptz_attention.profile.edit.active", {}, "Pause the active session before editing this profile.")}</SessionNotice> : null}
        {liveResumeBlocked ? <SessionNotice tone="warning">{t("ext.ptz_attention.profile.resume.blocked", {}, "Live movement cannot resume until the readiness check succeeds.")}</SessionNotice> : null}
        {canConfigure && !editBlocked && !deleteAllowed ? <SessionNotice tone="neutral">{t("ext.ptz_attention.profile.delete.blocked", {}, "Pause or disable the profile and wait for the camera to be released before deleting it.")}</SessionNotice> : null}

        <div className="ptzAttentionMetrics">
          <Metric label={t("ext.ptz_attention.session.event", {}, "Event")} value={activeEvent} />
          <Metric label={t("ext.ptz_attention.session.camera", {}, "PTZ camera")} value={camera?.name || profile.camera_id} />
          <Metric
            label={t("ext.ptz_attention.session.view", {}, "Desired view")}
            value={activeView?.label || (state && ["IDLE", "RETURNING"].includes(state) ? viewLabel(camera, profile.home_view_id) : "-")}
          />
          <Metric
            label={t("ext.ptz_attention.session.owner", {}, "Control owner")}
            value={session?.lease_active
              ? t("ext.ptz_attention.session.owner.attention", {}, "PTZ Attention")
              : state === "MANUAL_OVERRIDE"
                ? t("ext.ptz_attention.session.owner.manual", {}, "Manual")
                : "-"}
          />
          <Metric label={t("ext.ptz_attention.session.priority", {}, "Priority")} value={session?.active_priority === null || session?.active_priority === undefined ? "-" : String(session.active_priority)} />
          <Metric label={t("ext.ptz_attention.session.state_since", {}, "State since")} value={session?.state_since ? formatTime(session.state_since, locale) : "-"} />
        </div>

        <div className="ptzAttentionActions">
          {profile.mode === "paused" || session?.paused ? (
            <button className="primaryButton" type="button" disabled={controlsDisabled || liveResumeBlocked} onClick={() => onCommand("resume")}>
              {t("ext.ptz_attention.action.resume", {}, "Resume automation")}
            </button>
          ) : (
            <button className="chipButton" type="button" disabled={controlsDisabled || profile.mode === "disabled" || state === "MANUAL_OVERRIDE" || state === "FAULT"} onClick={() => onCommand("pause")}>
              {t("ext.ptz_attention.action.pause", {}, "Pause here")}
            </button>
          )}
          <button className="chipButton" type="button" disabled={controlsDisabled || state === "IDLE" || state === "MANUAL_OVERRIDE"} onClick={() => onCommand("return-home")}>
            {state === "FAULT"
              ? t("ext.ptz_attention.action.retry_return", {}, "Try return again")
              : t("ext.ptz_attention.action.return_home", {}, "Return home now")}
          </button>
          {canConfigure ? (
            <button
              className="dangerButton"
              type="button"
              disabled={!deleteAllowed || busy}
              title={!deleteAllowed ? t("ext.ptz_attention.profile.delete.blocked", {}, "Pause or disable the profile and wait for the camera to be released before deleting it.") : undefined}
              onClick={onDelete}
            >
              {t("ext.ptz_attention.profile.delete.action", {}, "Delete profile")}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function DecisionReview({
  t,
  locale,
  decisions,
  camera,
  loading,
  error,
  hasMore,
  paging,
  onLoadMore,
}: {
  t: Translate;
  locale: string;
  decisions: AttentionDecision[];
  camera: AttentionCameraCatalogItem | null;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  paging: boolean;
  onLoadMore: () => void;
}): React.ReactElement {
  return (
    <div className="ptzAttentionCard">
      <div className="ptzAttentionCardBody">
        <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.decisions.title", {}, "Shadow and decision review")}</h3>
        {error ? (
          <div className="ptzAttentionNotice" data-tone="warning" role="status" style={{ marginTop: 10 }}>
            <strong>{t("ext.ptz_attention.decisions.unavailable", {}, "Decision history is temporarily unavailable.")}</strong>
            <div className="ptzAttentionSubtle" style={{ marginTop: 4 }}>{error}</div>
          </div>
        ) : null}
        {loading ? (
          <div className="ptzAttentionMuted" role="status" style={{ marginTop: 10 }}>{t("ext.ptz_attention.decisions.loading", {}, "Loading decisions...")}</div>
        ) : !error && decisions.length === 0 ? (
          <div className="ptzAttentionMuted" style={{ marginTop: 10 }}>{t("ext.ptz_attention.decisions.empty", {}, "No decision recorded.")}</div>
        ) : decisions.length > 0 ? (
          <div className="ptzAttentionDecisionList">
            {decisions.map((decision) => {
              const decisionViewId = typeof decision.details.view_id === "string" ? decision.details.view_id : "";
              const view = camera?.views.find((item) => item.id === decisionViewId) ?? null;
              return (
                <div className="ptzAttentionDecision" key={decision.id}>
                  <span className="ptzAttentionDecisionMark" data-tone={decisionTone(decision)} aria-hidden="true" />
                  <div>
                    <div className="ptzAttentionProfileName">{decisionActionLabel(decision.action, t)}</div>
                    <div className="ptzAttentionMuted">
                      {[
                        decision.pipeline_name,
                        decision.reason ? decisionReasonLabel(decision.reason, t) : "",
                        decision.event_key,
                        view?.label,
                      ].filter(Boolean).join(" · ") || "-"}
                    </div>
                  </div>
                  <time className="ptzAttentionDecisionTime" dateTime={new Date(decision.created_at * 1000).toISOString()}>{formatDateTime(decision.created_at, locale)}</time>
                </div>
              );
            })}
          </div>
        ) : null}
        {hasMore && !loading ? (
          <div className="ptzAttentionActions">
            <button className="chipButton" type="button" disabled={paging} onClick={onLoadMore}>
              {paging
                ? t("ext.ptz_attention.decisions.loading_more", {}, "Loading earlier decisions...")
                : t("ext.ptz_attention.decisions.load_more", {}, "Load earlier decisions")}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function DiagnosticsCard({
  t,
  issues,
  validation,
  loading,
  error,
  session,
  onRetry,
  disabled,
}: {
  t: Translate;
  issues: AttentionReadinessIssue[];
  validation: AttentionValidationResponse | null;
  loading: boolean;
  error: string | null;
  session: AttentionSession | null;
  onRetry: () => void;
  disabled: boolean;
}): React.ReactElement {
  const runtimeFault = session?.fault || "";
  return (
    <div className="ptzAttentionCard">
      <div className="ptzAttentionCardBody">
        <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.diagnostics.title", {}, "Readiness and faults")}</h3>
        {runtimeFault ? (
          <div className="ptzAttentionIssueList">
            <div className="ptzAttentionNotice" data-tone="danger">
              <strong>{t("ext.ptz_attention.fault.code", {}, "Fault code")}: {decisionReasonLabel(runtimeFault, t)}</strong>
              <div className="ptzAttentionActions"><button className="chipButton" type="button" disabled={disabled} onClick={onRetry}>{t("ext.ptz_attention.action.retry_return", {}, "Try return again")}</button></div>
            </div>
          </div>
        ) : null}
        {error ? (
          <div className="ptzAttentionNotice" data-tone="warning" role="status" style={{ marginTop: 10 }}>
            <strong>{t("ext.ptz_attention.diagnostics.unavailable", {}, "Readiness check is temporarily unavailable.")}</strong>
            <div className="ptzAttentionSubtle" style={{ marginTop: 4 }}>{error}</div>
          </div>
        ) : loading ? (
          <div className="ptzAttentionMuted" style={{ marginTop: 10 }}>{t("ext.ptz_attention.diagnostics.loading", {}, "Checking readiness...")}</div>
        ) : !runtimeFault && !validation ? (
          <div className="ptzAttentionMuted" style={{ marginTop: 10 }}>{t("ext.ptz_attention.diagnostics.empty", {}, "No diagnostic data.")}</div>
        ) : !runtimeFault && validation?.ok && issues.length === 0 ? (
          <div className="ptzAttentionMuted" style={{ marginTop: 10 }}>{t("ext.ptz_attention.diagnostics.ready", {}, "No blocking issue.")}</div>
        ) : validation && (validation.automation_ready !== true || issues.length > 0) ? (
          <div className="ptzAttentionIssueList">
            {validation && validation.automation_ready !== true && validation.automation_ready_reason ? (
              <div className="ptzAttentionNotice" data-tone="warning">
                <strong>{t("ext.ptz_attention.diagnostics.control", {}, "Camera control readiness")}</strong>
                <div className="ptzAttentionMuted" style={{ marginTop: 4 }}>{readinessIssueLabel(validation.automation_ready_reason, t)}</div>
              </div>
            ) : null}
            {issues.map((issue, index) => (
              <div className="ptzAttentionNotice" data-tone={issue.severity === "error" ? "danger" : issue.severity} key={`${issue.code}-${index}`}>
                <strong>{readinessIssueLabel(issue.code, t)}</strong>{issue.message ? ` · ${issue.message}` : ""}
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function EmptyState({ icon, title, description, actionLabel, onAction }: { icon: string; title: string; description: string; actionLabel?: string; onAction?: () => void }): React.ReactElement {
  return (
    <div className="ptzAttentionCard">
      <div className="ptzAttentionEmpty">
        <div className="ptzAttentionEmptyIcon"><i className={`fa-solid ${icon}`} aria-hidden="true" /></div>
        <h3 className="ptzAttentionCardTitle">{title}</h3>
        <div className="ptzAttentionMuted" style={{ maxWidth: 560, margin: "8px auto 0" }}>{description}</div>
        {actionLabel && onAction ? <button className="primaryButton" type="button" style={{ marginTop: 16 }} onClick={onAction}>{actionLabel}</button> : null}
      </div>
    </div>
  );
}

function SessionNotice({ tone, children }: { tone: Tone; children: React.ReactNode }): React.ReactElement {
  return <div className="ptzAttentionNotice" data-tone={tone} role="status" style={{ marginTop: 14 }}>{children}</div>;
}

function StatusBadge({ tone, children }: { tone: Tone; children: React.ReactNode }): React.ReactElement {
  return <span className="ptzAttentionBadge" data-tone={tone === "neutral" ? undefined : tone}>{children}</span>;
}

function Metric({ label, value }: { label: string; value: string }): React.ReactElement {
  return <div className="ptzAttentionMetric"><div className="ptzAttentionSubtle">{label}</div><div className="ptzAttentionMetricValue">{value}</div></div>;
}

function timelineIndex(state: AttentionRuntimeState): number {
  if (state === "CANDIDATE" || state === "ACQUIRING") return 1;
  if (state === "FOCUSED" || state === "GRACE") return 2;
  if (state === "RETURNING") return 3;
  if (state === "IDLE") return 0;
  return -1;
}

function stateLabel(state: AttentionRuntimeState, t: Translate): string {
  return t(`ext.ptz_attention.state.${state.toLowerCase()}`, {}, state.toLowerCase().replace(/_/g, " "));
}

function modeLabel(mode: AttentionExecutionMode, t: Translate): string {
  return t(`ext.ptz_attention.profile.mode.${mode}`, {}, mode.replace(/_/g, " "));
}

function stateTone(state: AttentionRuntimeState): Tone {
  if (state === "FAULT") return "danger";
  if (state === "MANUAL_OVERRIDE" || state === "GRACE") return "warning";
  if (state === "FOCUSED") return "success";
  if (state === "ACQUIRING" || state === "RETURNING" || state === "CANDIDATE") return "info";
  return "neutral";
}

function modeTone(mode: AttentionExecutionMode): Tone {
  if (mode === "live_preset") return "success";
  if (mode === "shadow") return "info";
  if (mode === "paused") return "warning";
  return "neutral";
}

function decisionTone(decision: AttentionDecision): Tone {
  const action = decision.action.toLowerCase();
  if (decision.state === "FAULT" || action.includes("fault") || action.includes("failed")) return "danger";
  if (action.includes("suppress") || action.includes("reject") || action.includes("shadow")) return "warning";
  if (action.includes("focus") || action.includes("home") || action.includes("return")) return "success";
  return "neutral";
}

function decisionActionLabel(action: string, t: Translate): string {
  const normalized = String(action || "").trim().toLowerCase();
  return t(`ext.ptz_attention.decision.${normalized}`, {}, normalized.replace(/_/g, " ") || "-");
}

function decisionReasonLabel(reason: string, t: Translate): string {
  const normalized = String(reason || "").trim().toLowerCase().split(":", 1)[0];
  return t(`ext.ptz_attention.reason.${normalized}`, {}, normalized.replace(/_/g, " ") || "-");
}

function readinessIssueLabel(code: string, t: Translate): string {
  const normalized = String(code || "").trim().toLowerCase();
  return t(`ext.ptz_attention.issue.${normalized}`, {}, normalized.replace(/_/g, " ") || "-");
}

function currentViewId(session: AttentionSession | null): string {
  return session?.active_view_id || session?.candidate_view_id || "";
}

function viewLabel(camera: AttentionCameraCatalogItem | null, viewId: string): string {
  return camera?.views?.find((view) => view.id === viewId)?.label || viewId || "-";
}

function formatTime(value: number, locale: string): string {
  if (!Number.isFinite(value) || value <= 0) return "-";
  return new Intl.DateTimeFormat(locale || undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value * 1000));
}

function formatDateTime(value: number, locale: string): string {
  if (!Number.isFinite(value) || value <= 0) return "-";
  return new Intl.DateTimeFormat(locale || undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value * 1000));
}

function normalizeProfiles(payload: unknown): AttentionProfile[] {
  if (Array.isArray(payload)) return payload as AttentionProfile[];
  if (payload && typeof payload === "object" && Array.isArray((payload as { profiles?: unknown }).profiles)) {
    return (payload as { profiles: AttentionProfile[] }).profiles;
  }
  return [];
}

function normalizeStatus(payload: AttentionStatusResponse): AttentionStatusResponse {
  return { ...payload, devices: Array.isArray(payload.devices) ? payload.devices : [] };
}

function normalizeDecisions(decisions: AttentionDecision[]): AttentionDecision[] {
  return Array.isArray(decisions) ? decisions : [];
}

function normalizeCursor(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function mergeDecisions(...groups: AttentionDecision[][]): AttentionDecision[] {
  const records = new Map<string, AttentionDecision>();
  for (const decision of groups.flat()) records.set(decision.id || String(decision.seq), decision);
  return Array.from(records.values()).sort((left, right) => right.seq - left.seq);
}
