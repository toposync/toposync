import React, { useEffect, useMemo, useState } from "react";
import type { HostI18n } from "@toposync/plugin-api";

import type {
  AttentionCameraCatalogItem,
  AttentionCatalogResponse,
  AttentionBindingCatalogItem,
  AttentionEventPolicy,
  AttentionEventTypeCatalogItem,
  AttentionExecutionMode,
  AttentionProfile,
  AttentionValidationResponse,
  AttentionViewCatalogItem,
} from "../types";
import { SubModal } from "./SubModal";

type Translate = ReturnType<HostI18n["useI18n"]>["t"];

const DEFAULT_PROFILE: AttentionProfile = {
  id: "attention_profile",
  name: "",
  mode: "shadow",
  resume_mode: null,
  camera_id: "",
  source_id: "",
  ptz_device_id: "",
  same_head_observer_acknowledged: false,
  composition_id: "",
  home_view_id: "",
  eligible_view_ids: [],
  event_policies: [],
  candidate_confirm_seconds: 3,
  min_focus_seconds: 10,
  max_focus_seconds: 120,
  close_grace_seconds: 6,
  cooldown_seconds: 10,
  stale_timeout_seconds: 18,
  settle_timeout_seconds: 8,
  lease_ttl_seconds: 15,
  max_movements_per_minute: 2,
  minimum_target_confidence: 0,
};

export function PtzAttentionWizard({
  open,
  i18n,
  catalog,
  existingProfiles,
  initialProfile,
  onClose,
  onValidate,
  onSave,
}: {
  open: boolean;
  i18n: HostI18n;
  catalog: AttentionCatalogResponse | null;
  existingProfiles: AttentionProfile[];
  initialProfile: AttentionProfile | null;
  onClose: () => void;
  onValidate: (profile: AttentionProfile, signal?: AbortSignal) => Promise<AttentionValidationResponse>;
  onSave: (profile: AttentionProfile) => Promise<void>;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [step, setStep] = useState(0);
  const [profile, setProfile] = useState<AttentionProfile>(DEFAULT_PROFILE);
  const [validation, setValidation] = useState<AttentionValidationResponse | null>(null);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setStep(0);
    setProfile(initialProfile ? cloneProfile(initialProfile) : { ...DEFAULT_PROFILE, event_policies: [], eligible_view_ids: [] });
    setValidation(null);
    setValidating(false);
    setSaving(false);
    setError(null);
  }, [initialProfile, open]);

  const eventTypes = Array.isArray(catalog?.event_types) ? catalog.event_types : [];
  const bindings = Array.isArray(catalog?.bindings) ? catalog.bindings : [];
  const cameras = Array.isArray(catalog?.cameras) ? catalog.cameras : [];
  const occupiedPtzDeviceIds = useMemo(
    () => new Set(
      existingProfiles
        .filter((item) => item.id !== initialProfile?.id)
        .map((item) => item.ptz_device_id)
        .filter(Boolean),
    ),
    [existingProfiles, initialProfile?.id],
  );
  const selectedCamera = cameras.find((camera) => camera.id === profile.camera_id) ?? null;
  const cameraViews = selectedCamera?.views ?? [];
  const compositions = useMemo(() => uniqueCompositions(selectedCamera?.views ?? []), [selectedCamera]);
  const readyViews = useMemo(
    () => uniqueReadyViews((selectedCamera?.views ?? []).filter(
      (view) =>
        (!profile.composition_id || view.composition_id === profile.composition_id) &&
        viewSupportsSource(view, selectedCamera, profile.source_id),
    )),
    [profile.composition_id, profile.source_id, selectedCamera],
  );
  const selectedBindings = bindings.filter(
    (binding) =>
      binding.enabled &&
      binding.profile_id === profile.id &&
      profile.event_policies.some(
        (policy) =>
          policy.enabled &&
          (binding.event_type ? policy.event_type === binding.event_type : Boolean(binding.event_type_field)),
      ),
  );
  const sharedObserver = selectedBindings.some(
    (binding) => Boolean(binding.observer_camera_id) && binding.observer_camera_id === profile.camera_id,
  );
  const sharedObserverAcknowledged = profile.same_head_observer_acknowledged;

  useEffect(() => {
    if (!open || step !== 3 || !canValidateProfile(
      profile,
      selectedCamera,
      readyViews,
      sharedObserver,
      sharedObserverAcknowledged,
    )) {
      setValidation(null);
      setValidating(false);
      return;
    }
    const controller = new AbortController();
    setValidation(null);
    setValidating(true);
    setError(null);
    const timer = window.setTimeout(() => {
      onValidate(profile, controller.signal)
        .then((result) => {
          if (!controller.signal.aborted) {
            setValidation(result);
            setError(null);
          }
        })
        .catch((reason) => {
          if (!controller.signal.aborted) {
            setValidation(null);
            setError(reason instanceof Error ? reason.message : String(reason));
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setValidating(false);
        });
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [onValidate, open, profile, readyViews, selectedCamera, sharedObserver, sharedObserverAcknowledged, step]);

  const steps = [
    t("ext.ptz_attention.wizard.step.events", {}, "Events"),
    t("ext.ptz_attention.wizard.step.camera", {}, "Camera and views"),
    t("ext.ptz_attention.wizard.step.policy", {}, "Policy"),
    t("ext.ptz_attention.wizard.step.safety", {}, "Safety"),
  ];

  const stepError = validateStep(step, profile, {
    selectedCamera,
    readyViews,
    sharedObserver,
    sharedObserverAcknowledged,
    t,
  });

  function selectCamera(cameraId: string): void {
    const camera = cameras.find((item) => item.id === cameraId) ?? null;
    if (camera && occupiedPtzDeviceIds.has(camera.id)) {
      setError(t("ext.ptz_attention.wizard.camera.in_use", {}, "This PTZ camera already belongs to another attention profile."));
      return;
    }
    if (camera && !(catalog?.permissions.configure || camera.permissions.configure)) {
      setError(t("ext.ptz_attention.permission.configure", {}, "Profile configuration permission is required."));
      return;
    }
    const enabledVideoSources = (camera?.sources ?? []).filter(
      (source) => source.enabled && source.kind === "video" && source.has_ptz,
    );
    const sourceId = enabledVideoSources.find((source) => source.id === camera?.control_source_id)?.id
      || enabledVideoSources.find((source) => source.is_default)?.id
      || enabledVideoSources[0]?.id
      || "";
    const nextViews = (camera?.views ?? []).filter((view) => viewSupportsSource(view, camera, sourceId));
    const firstReadyView = uniqueReadyViews(nextViews)[0] ?? null;
    setProfile((previous) => ({
      ...previous,
      camera_id: camera?.id ?? "",
      source_id: sourceId,
      ptz_device_id: camera?.id || "",
      same_head_observer_acknowledged: false,
      composition_id: firstReadyView?.composition_id ?? "",
      home_view_id: firstReadyView?.id ?? "",
      eligible_view_ids: firstReadyView ? [firstReadyView.id] : [],
      event_policies: previous.event_policies.map((policy) => ({ ...policy, preferred_view_id: "" })),
    }));
  }

  function selectSource(sourceId: string): void {
    const safeSourceId = (selectedCamera?.sources ?? []).some(
      (source) => source.id === sourceId && source.enabled && source.kind === "video" && source.has_ptz,
    ) ? sourceId : "";
    const nextViews = uniqueReadyViews(cameraViews.filter((view) =>
      viewSupportsSource(view, selectedCamera, safeSourceId),
    ));
    const first = nextViews[0] ?? null;
    setProfile((previous) => ({
      ...previous,
      source_id: safeSourceId,
      composition_id: first?.composition_id ?? previous.composition_id,
      home_view_id: first?.id ?? "",
      eligible_view_ids: first ? [first.id] : [],
      event_policies: previous.event_policies.map((policy) => ({ ...policy, preferred_view_id: "" })),
    }));
  }

  function selectComposition(compositionId: string): void {
    const nextViews = uniqueReadyViews(cameraViews.filter(
      (view) =>
        view.composition_id === compositionId &&
        viewSupportsSource(view, selectedCamera, profile.source_id),
    ));
    const first = nextViews[0] ?? null;
    setProfile((previous) => ({
      ...previous,
      composition_id: compositionId,
      home_view_id: first?.id ?? "",
      eligible_view_ids: first ? [first.id] : [],
      event_policies: previous.event_policies.map((policy) => ({ ...policy, preferred_view_id: "" })),
    }));
  }

  function toggleEvent(event: AttentionEventTypeCatalogItem, checked: boolean): void {
    setProfile((previous) => {
      const remaining = previous.event_policies.filter((policy) => policy.event_type !== event.event_type);
      return {
        ...previous,
        same_head_observer_acknowledged: false,
        event_policies: checked
          ? [...remaining, { event_type: event.event_type, enabled: true, priority: 0, preferred_view_id: "" }]
          : remaining,
      };
    });
  }

  function addEventType(eventType: string): void {
    const normalized = eventType.trim();
    if (!normalized) return;
    setProfile((previous) => previous.event_policies.some((policy) => policy.event_type === normalized)
      ? previous
      : {
          ...previous,
          event_policies: [
            ...previous.event_policies,
            { event_type: normalized, enabled: true, priority: 0, preferred_view_id: "" },
          ],
        });
  }

  function removeEventType(eventType: string): void {
    setProfile((previous) => ({
      ...previous,
      same_head_observer_acknowledged: false,
      event_policies: previous.event_policies.filter((policy) => policy.event_type !== eventType),
    }));
  }

  function updateEventPolicy(eventType: string, patch: Partial<AttentionEventPolicy>): void {
    setProfile((previous) => ({
      ...previous,
      event_policies: previous.event_policies.map((policy) =>
        policy.event_type === eventType ? { ...policy, ...patch } : policy,
      ),
    }));
  }

  function toggleEligibleView(viewId: string, checked: boolean): void {
    setProfile((previous) => ({
      ...previous,
      eligible_view_ids: checked
        ? Array.from(new Set([...previous.eligible_view_ids, viewId]))
        : previous.eligible_view_ids.filter((item) => item !== viewId),
      event_policies: checked
        ? previous.event_policies
        : previous.event_policies.map((policy) =>
            policy.preferred_view_id === viewId ? { ...policy, preferred_view_id: "" } : policy,
          ),
    }));
  }

  function advance(): void {
    if (stepError) {
      setError(stepError);
      return;
    }
    setError(null);
    setStep((current) => Math.min(3, current + 1));
  }

  async function save(): Promise<void> {
    const firstError = [0, 1, 2, 3]
      .map((index) => validateStep(index, profile, {
        selectedCamera,
        readyViews,
        sharedObserver,
        sharedObserverAcknowledged,
        t,
      }))
      .find(Boolean);
    if (firstError) {
      setError(firstError);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const validated = await onValidate(profile);
      setValidation(validated);
      const blocking = validated.issues.some((issue) => issue.blocking || issue.severity === "error");
      if (!validated.ok || blocking) return;
      await onSave(profile);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <SubModal
      open={open}
      busy={saving}
      closeLabel={t("ext.ptz_attention.action.close", {}, "Close")}
      title={t(
        initialProfile ? "ext.ptz_attention.wizard.title.edit" : "ext.ptz_attention.wizard.title.create",
        {},
        initialProfile ? "Edit PTZ Attention profile" : "Create PTZ Attention profile",
      )}
      onClose={onClose}
    >
      <div className="ptzAttentionWizardSteps" aria-label={t("ext.ptz_attention.wizard.steps_aria", {}, "Wizard steps")}>
        {steps.map((label, index) => (
          <button
            key={label}
            className="ptzAttentionWizardStep"
            type="button"
            data-current={step === index}
            aria-current={step === index ? "step" : undefined}
            disabled={index > step}
            onClick={() => setStep(index)}
          >
            <span className="ptzAttentionSubtle">{index + 1}/4</span>
            <div className="ptzAttentionChoiceTitle">{label}</div>
          </button>
        ))}
      </div>

      <div className="ptzAttentionWizardBody">
        {step === 0 ? (
          <EventsStep
            t={t}
            eventTypes={eventTypes}
            bindings={bindings}
            profileId={profile.id}
            policies={profile.event_policies}
            onToggle={toggleEvent}
            onAddType={addEventType}
            onRemoveType={removeEventType}
            onUpdatePolicy={updateEventPolicy}
          />
        ) : null}
        {step === 1 ? (
          <CameraStep
            t={t}
            cameras={cameras}
            globalConfigure={catalog?.permissions.configure === true}
            profile={profile}
            selectedCamera={selectedCamera}
            occupiedPtzDeviceIds={occupiedPtzDeviceIds}
            compositions={compositions}
            readyViews={readyViews}
            sharedObserver={sharedObserver}
            sharedObserverAcknowledged={sharedObserverAcknowledged}
            onSelectCamera={selectCamera}
            onSelectSource={selectSource}
            onSelectComposition={selectComposition}
            onSetHome={(home_view_id) => setProfile((previous) => {
              const eligibleWithoutPreviousHome = previous.eligible_view_ids.filter(
                (viewId) => viewId !== previous.home_view_id,
              );
              return {
                ...previous,
                home_view_id,
                eligible_view_ids: home_view_id
                  ? Array.from(new Set([home_view_id, ...eligibleWithoutPreviousHome]))
                  : eligibleWithoutPreviousHome,
                event_policies: previous.event_policies.map((policy) =>
                  policy.preferred_view_id === previous.home_view_id || policy.preferred_view_id === home_view_id
                    ? { ...policy, preferred_view_id: "" }
                    : policy,
                ),
              };
            })}
            onToggleView={toggleEligibleView}
            onAcknowledgeShared={(checked) => setProfile((previous) => ({
              ...previous,
              same_head_observer_acknowledged: checked,
            }))}
            onUpdatePolicy={updateEventPolicy}
          />
        ) : null}
        {step === 2 ? <PolicyStep t={t} profile={profile} onChange={setProfile} /> : null}
        {step === 3 ? (
          <SafetyStep
            t={t}
            profile={profile}
            bindingEvents={selectedBindings}
            camera={selectedCamera}
            views={readyViews}
            validation={validation}
            validating={validating}
            onMode={(mode) => setProfile((previous) => ({ ...previous, mode, resume_mode: null }))}
            onName={(name) => setProfile((previous) => ({ ...previous, name, id: initialProfile?.id ?? profileIdFromName(name) }))}
          />
        ) : null}
        {error || (step === 3 && stepError) ? (
          <div className="errorText" role="alert">{error || stepError}</div>
        ) : null}
      </div>

      <div className="ptzAttentionWizardFooter">
        <div className="ptzAttentionWizardFooterGroup">
          <button className="chipButton" type="button" disabled={saving} onClick={onClose}>
            {t("ext.ptz_attention.action.cancel", {}, "Cancel")}
          </button>
        </div>
        <div className="ptzAttentionWizardFooterGroup">
          {step > 0 ? (
            <button className="chipButton" type="button" disabled={saving} onClick={() => { setError(null); setStep((current) => current - 1); }}>
              {t("ext.ptz_attention.action.back", {}, "Back")}
            </button>
          ) : null}
          {step < 3 ? (
            <button className="primaryButton" type="button" onClick={advance}>
              {t("ext.ptz_attention.action.next", {}, "Next")}
            </button>
          ) : (
            <button className="primaryButton" type="button" disabled={saving || validating || Boolean(stepError) || validation?.ok === false} onClick={() => void save()}>
              {saving
                ? t("ext.ptz_attention.action.saving", {}, "Saving...")
                : t("ext.ptz_attention.action.save", {}, "Save profile")}
            </button>
          )}
        </div>
      </div>
    </SubModal>
  );
}

function EventsStep({
  t,
  eventTypes,
  bindings,
  profileId,
  policies,
  onToggle,
  onAddType,
  onRemoveType,
  onUpdatePolicy,
}: {
  t: Translate;
  eventTypes: AttentionEventTypeCatalogItem[];
  bindings: AttentionBindingCatalogItem[];
  profileId: string;
  policies: AttentionEventPolicy[];
  onToggle: (event: AttentionEventTypeCatalogItem, checked: boolean) => void;
  onAddType: (eventType: string) => void;
  onRemoveType: (eventType: string) => void;
  onUpdatePolicy: (eventType: string, patch: Partial<AttentionEventPolicy>) => void;
}): React.ReactElement {
  const [customType, setCustomType] = useState("");
  const recognized = Array.from(
    new Map(eventTypes.filter((event) => event.event_type).map((event) => [event.event_type, event])).values(),
  );
  const suggestions = recognized.filter(
    (event) => !policies.some((policy) => policy.event_type === event.event_type),
  );

  function submitCustom(event: React.FormEvent): void {
    event.preventDefault();
    const normalized = customType.trim();
    if (!normalized) return;
    onAddType(normalized);
    setCustomType("");
  }

  return (
    <section>
      <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.wizard.events.title", {}, "Which events deserve the camera?")}</h3>
      <div className="ptzAttentionMuted">{t("ext.ptz_attention.wizard.events.desc", {}, "Only explicit semantic event sources can request PTZ focus.")}</div>
      <div className="ptzAttentionNotice" data-tone="info" style={{ marginTop: 12 }}>
        {t(
          "ext.ptz_attention.wizard.events.binding_note",
          {},
          "This step authorizes event types. A pipeline still needs a PTZ Attention request operator bound to this profile; the wizard does not edit pipelines.",
        )}
      </div>

      {suggestions.length ? (
        <div style={{ marginTop: 14 }}>
          <div className="ptzAttentionSubtle">{t("ext.ptz_attention.wizard.events.recognized", {}, "Recognized in pipelines")}</div>
          <div className="ptzAttentionInlineActions" style={{ marginTop: 8 }}>
            {suggestions.map((event) => (
              <button className="chipButton" key={event.event_type} type="button" onClick={() => onToggle(event, true)}>
                <i className="fa-solid fa-plus" aria-hidden="true" />
                <span>{event.event_type}</span>
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <form className="ptzAttentionInlineActions" style={{ marginTop: 14 }} onSubmit={submitCustom}>
        <label className="ptzAttentionField" style={{ flex: "1 1 260px" }}>
          <span>{t("ext.ptz_attention.wizard.events.custom", {}, "Event type")}</span>
          <input
            className="input"
            value={customType}
            onChange={(event) => setCustomType(event.target.value)}
            placeholder={t("ext.ptz_attention.wizard.events.custom_placeholder", {}, "security.person_near_vehicle")}
            autoComplete="off"
          />
        </label>
        <button className="chipButton" type="submit" disabled={!customType.trim()}>
          {t("ext.ptz_attention.wizard.events.add", {}, "Add event")}
        </button>
      </form>

      {policies.length === 0 ? (
        <div className="ptzAttentionNotice" data-tone="warning" style={{ marginTop: 14 }}>
          {t("ext.ptz_attention.wizard.events.none", {}, "No event type is authorized yet.")}
        </div>
      ) : (
        <div className="ptzAttentionChoiceGrid" style={{ marginTop: 12 }}>
          {policies.map((policy) => {
            const bound = bindings.some(
              (binding) =>
                binding.profile_id === profileId &&
                binding.enabled &&
                (binding.event_type === policy.event_type || (!binding.event_type && Boolean(binding.event_type_field))),
            );
            return (
              <div className="ptzAttentionChoice" data-selected={policy.enabled} key={policy.event_type}>
                <div className="ptzAttentionInlineActions" style={{ justifyContent: "space-between" }}>
                  <label className="ptzAttentionInlineActions">
                  <input
                    type="checkbox"
                      checked={policy.enabled}
                      onChange={(input) => onUpdatePolicy(policy.event_type, { enabled: input.target.checked })}
                  />
                    <span className="ptzAttentionChoiceTitle">{policy.event_type}</span>
                  </label>
                  <span className="ptzAttentionBadge" data-tone={bound ? "success" : "warning"}>
                    {bound
                      ? t("ext.ptz_attention.wizard.events.bound", {}, "Bound")
                      : t("ext.ptz_attention.wizard.events.unbound", {}, "Binding pending")}
                  </span>
                </div>
                <label className="ptzAttentionField" style={{ marginTop: 10 }}>
                  <span>{t("ext.ptz_attention.operator.priority", {}, "Attention priority")}</span>
                  <select className="input" value={String(policy.priority)} onChange={(input) => onUpdatePolicy(policy.event_type, { priority: Number(input.target.value) })}>
                    <option value="-100">{t("ext.ptz_attention.priority.low", {}, "Low")}</option>
                    <option value="0">{t("ext.ptz_attention.priority.medium", {}, "Medium")}</option>
                    <option value="100">{t("ext.ptz_attention.priority.high", {}, "High")}</option>
                  </select>
                </label>
                <button className="chipButton" type="button" style={{ marginTop: 10 }} onClick={() => onRemoveType(policy.event_type)}>
                  {t("ext.ptz_attention.wizard.events.remove", {}, "Remove")}
                </button>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function CameraStep({
  t,
  cameras,
  globalConfigure,
  profile,
  selectedCamera,
  occupiedPtzDeviceIds,
  compositions,
  readyViews,
  sharedObserver,
  sharedObserverAcknowledged,
  onSelectCamera,
  onSelectSource,
  onSelectComposition,
  onSetHome,
  onToggleView,
  onAcknowledgeShared,
  onUpdatePolicy,
}: {
  t: Translate;
  cameras: AttentionCameraCatalogItem[];
  globalConfigure: boolean;
  profile: AttentionProfile;
  selectedCamera: AttentionCameraCatalogItem | null;
  occupiedPtzDeviceIds: ReadonlySet<string>;
  compositions: Array<{ id: string; name: string }>;
  readyViews: AttentionViewCatalogItem[];
  sharedObserver: boolean;
  sharedObserverAcknowledged: boolean;
  onSelectCamera: (cameraId: string) => void;
  onSelectSource: (sourceId: string) => void;
  onSelectComposition: (compositionId: string) => void;
  onSetHome: (viewId: string) => void;
  onToggleView: (viewId: string, checked: boolean) => void;
  onAcknowledgeShared: (checked: boolean) => void;
  onUpdatePolicy: (eventType: string, patch: Partial<AttentionEventPolicy>) => void;
}): React.ReactElement {
  const eligibleViews = readyViews.filter(
    (view) => view.id !== profile.home_view_id && profile.eligible_view_ids.includes(view.id),
  );
  const cameraIssues = selectedCamera
    ? [
        !selectedCamera.enabled ? "camera_disabled" : "",
        !selectedCamera.actuator_id ? "ptz_control_unavailable" : "",
        !selectedCamera.sources.some((source) => source.enabled && source.kind === "video" && source.has_ptz)
          ? "video_source_unavailable"
          : "",
        selectedCamera.actuator_id && cameras.filter((camera) => camera.actuator_id === selectedCamera.actuator_id).length > 1
          ? "shared_ptz_actuator"
          : "",
      ].filter(Boolean)
    : [];
  return (
    <section>
      <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.wizard.camera.title", {}, "Where can the camera focus?")}</h3>
      <div className="ptzAttentionMuted">{t("ext.ptz_attention.wizard.camera.desc", {}, "Choose a physical PTZ camera and calibrated views.")}</div>
      <div className="ptzAttentionFields" style={{ marginTop: 12 }}>
        <label className="ptzAttentionField">
          <span>{t("ext.ptz_attention.wizard.camera.label", {}, "PTZ camera")}</span>
          <select className="input" value={profile.camera_id} onChange={(event) => onSelectCamera(event.target.value)}>
            <option value="">{t("ext.ptz_attention.wizard.camera.placeholder", {}, "Select a camera")}</option>
            {cameras.map((camera) => {
              const inUse = occupiedPtzDeviceIds.has(camera.id);
              const canConfigureCamera = globalConfigure || camera.permissions.configure;
              return (
              <option key={camera.id} value={camera.id} disabled={!camera.enabled || !camera.actuator_id || inUse || !canConfigureCamera}>
                {camera.name || camera.id}{inUse ? ` · ${t("ext.ptz_attention.wizard.camera.in_use_short", {}, "in use")}` : ""}
              </option>
              );
            })}
          </select>
        </label>
        <label className="ptzAttentionField">
          <span>{t("ext.ptz_attention.wizard.camera.source", {}, "Observation source")}</span>
          <select className="input" value={profile.source_id} disabled={!selectedCamera} onChange={(event) => onSelectSource(event.target.value)}>
            <option value="">{t("ext.ptz_attention.wizard.camera.source_placeholder", {}, "Select a video source")}</option>
            {(selectedCamera?.sources ?? []).filter(
              (source) => source.enabled && source.kind === "video" && source.has_ptz,
            ).map((source) => (
              <option key={source.id} value={source.id}>{source.name || source.id}</option>
            ))}
          </select>
        </label>
        <label className="ptzAttentionField">
          <span>{t("ext.ptz_attention.wizard.composition", {}, "Composition")}</span>
          <select className="input" value={profile.composition_id} disabled={!selectedCamera} onChange={(event) => onSelectComposition(event.target.value)}>
            <option value="">-</option>
            {compositions.map((composition) => <option key={composition.id} value={composition.id}>{composition.name}</option>)}
          </select>
        </label>
        <label className="ptzAttentionField">
          <span>{t("ext.ptz_attention.wizard.home", {}, "Home view")}</span>
          <select className="input" value={profile.home_view_id} disabled={!selectedCamera} onChange={(event) => onSetHome(event.target.value)}>
            <option value="">{t("ext.ptz_attention.wizard.home.placeholder", {}, "Select a calibrated home view")}</option>
            {readyViews.map((view) => <option key={view.id} value={view.id}>{view.label}</option>)}
          </select>
        </label>
      </div>
      {cameras.length === 0 ? (
        <div className="ptzAttentionNotice" data-tone="warning" style={{ marginTop: 12 }}>
          {t("ext.ptz_attention.wizard.camera.none", {}, "No PTZ camera is available in the safe catalog.")}
        </div>
      ) : null}
      {selectedCamera && !selectedCamera.automation_exclusive_control_confirmed ? (
        <div className="ptzAttentionNotice" data-tone="warning" style={{ marginTop: 12 }}>
          <div>{t(
              "ext.ptz_attention.wizard.camera.exclusive_control",
              {},
              "Exclusive automation control is not confirmed. Live mode will remain blocked until native auto-tracking and monitor-point movement are disabled in Camera settings.",
            )}</div>
          {selectedCamera.automation_ready_reason ? (
            <div className="ptzAttentionMuted" style={{ marginTop: 4 }}>{readinessIssueLabel(selectedCamera.automation_ready_reason, t)}</div>
          ) : null}
        </div>
      ) : null}
      {cameraIssues.length ? (
        <div className="ptzAttentionNotice" data-tone="warning" style={{ marginTop: 12 }}>
          {cameraIssues.map((reason) => t(`ext.ptz_attention.catalog.issue.${reason}`, {}, reason.replace(/_/g, " "))).join(" · ")}
        </div>
      ) : null}
      {selectedCamera?.sources?.some((source) => source.enabled && source.kind === "video" && source.has_ptz) ? (
        <div className="ptzAttentionMuted" style={{ marginTop: 10 }}>
          {t(
            "ext.ptz_attention.wizard.camera.sources",
            {
              sources: selectedCamera.sources
                .filter((source) => source.enabled && source.kind === "video" && source.has_ptz)
                .map((source) => source.name || source.id)
                .join(", "),
            },
            "Sources moved by this actuator: {{sources}}",
          )}
        </div>
      ) : null}
      {sharedObserver ? (
        <div className="ptzAttentionNotice" data-tone="warning" style={{ marginTop: 12 }}>
          <div>{t("ext.ptz_attention.wizard.camera.shared", {}, "Observer and PTZ use the same physical camera.")}</div>
          <label className="ptzAttentionInlineActions" style={{ marginTop: 8 }}>
            <input type="checkbox" checked={sharedObserverAcknowledged} onChange={(event) => onAcknowledgeShared(event.target.checked)} />
            <span>{t("ext.ptz_attention.wizard.camera.shared_ack", {}, "I understand the observer view moves during focus.")}</span>
          </label>
        </div>
      ) : null}
      <h4 className="ptzAttentionCardTitle" style={{ marginTop: 18 }}>{t("ext.ptz_attention.wizard.views", {}, "Allowed focus views")}</h4>
      {readyViews.length === 0 ? (
        <div className="ptzAttentionNotice" data-tone="warning" style={{ marginTop: 10 }}>
          {t("ext.ptz_attention.wizard.views.none", {}, "No ready, pose-bound view exists for this camera.")}
        </div>
      ) : (
        <div className="ptzAttentionChoiceGrid" style={{ marginTop: 10 }}>
          {readyViews.map((view) => (
            <label className="ptzAttentionChoice" data-selected={profile.eligible_view_ids.includes(view.id)} key={view.id}>
              <div>
                <input
                  type="checkbox"
                  checked={profile.eligible_view_ids.includes(view.id)}
                  disabled={view.id === profile.home_view_id}
                  onChange={(event) => onToggleView(view.id, event.target.checked)}
                />
                <span className="ptzAttentionChoiceTitle">{view.label}</span>
              </div>
              <div className="ptzAttentionChoiceMeta">
                {view.preset_name || view.composition_name || view.composition_id} · {viewQualityLabel(view, t)}
              </div>
            </label>
          ))}
        </div>
      )}
      {eligibleViews.length && profile.event_policies.length ? (
        <details className="ptzAttentionAdvanced" style={{ marginTop: 14 }}>
          <summary>{t("ext.ptz_attention.operator.view", {}, "Preferred view by event")}</summary>
          <div className="ptzAttentionFields">
            {profile.event_policies.filter((policy) => policy.enabled).map((policy) => (
              <label className="ptzAttentionField" key={policy.event_type}>
                <span>{policy.event_type}</span>
                <select className="input" value={policy.preferred_view_id} onChange={(event) => onUpdatePolicy(policy.event_type, { preferred_view_id: event.target.value })}>
                  <option value="">{t("ext.ptz_attention.operator.target.auto", {}, "Automatic from event")}</option>
                  {eligibleViews.map((view) => <option key={view.id} value={view.id}>{view.label}</option>)}
                </select>
              </label>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

function PolicyStep({ t, profile, onChange }: { t: Translate; profile: AttentionProfile; onChange: (profile: AttentionProfile) => void }): React.ReactElement {
  const numberField = (key: keyof AttentionProfile, value: number, min: number, max: number, stepValue: number) => (
    <input
      className="input"
      type="number"
      min={min}
      max={max}
      step={stepValue}
      value={value}
      onChange={(event) => onChange({ ...profile, [key]: clampNumber(event.target.value, value, min, max) })}
    />
  );
  return (
    <section>
      <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.wizard.policy.title", {}, "How should attention behave?")}</h3>
      <div className="ptzAttentionMuted">{t("ext.ptz_attention.wizard.policy.desc", {}, "Stable defaults prevent rapid switching.")}</div>
      <div className="ptzAttentionFields" style={{ marginTop: 12 }}>
        <label className="ptzAttentionField">
          <span>{t("ext.ptz_attention.wizard.policy.minimum_focus", {}, "Minimum focus")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>
          {numberField("min_focus_seconds", profile.min_focus_seconds, 0, 3600, 1)}
        </label>
        <label className="ptzAttentionField">
          <span>{t("ext.ptz_attention.wizard.policy.close_grace", {}, "Grace after close")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>
          {numberField("close_grace_seconds", profile.close_grace_seconds, 0, 3600, 1)}
        </label>
      </div>
      <details className="ptzAttentionAdvanced" style={{ marginTop: 14 }}>
        <summary>{t("ext.ptz_attention.wizard.policy.advanced", {}, "Advanced timing and movement limits")}</summary>
        <div className="ptzAttentionFields">
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.maximum_focus", {}, "Hard focus timeout")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>{numberField("max_focus_seconds", profile.max_focus_seconds, 0.5, 7200, 1)}</label>
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.cooldown", {}, "Cooldown after movement")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>{numberField("cooldown_seconds", profile.cooldown_seconds, 0, 3600, 1)}</label>
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.candidate", {}, "Candidate confirmation")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>{numberField("candidate_confirm_seconds", profile.candidate_confirm_seconds, 0, 60, 0.5)}</label>
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.stale", {}, "Event stale timeout")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>{numberField("stale_timeout_seconds", profile.stale_timeout_seconds, 0.5, 3600, 0.5)}</label>
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.settle", {}, "Settle timeout")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>{numberField("settle_timeout_seconds", profile.settle_timeout_seconds, 0.5, 120, 0.5)}</label>
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.lease", {}, "Control lease duration")} ({t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")})</span>{numberField("lease_ttl_seconds", profile.lease_ttl_seconds, 3, 300, 1)}</label>
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.maximum_moves", {}, "Maximum movements per minute")}</span>{numberField("max_movements_per_minute", profile.max_movements_per_minute, 1, 120, 1)}</label>
          <label className="ptzAttentionField"><span>{t("ext.ptz_attention.wizard.policy.confidence", {}, "Minimum target confidence")}</span>{numberField("minimum_target_confidence", profile.minimum_target_confidence, 0, 1, 0.05)}</label>
        </div>
      </details>
    </section>
  );
}

function SafetyStep({
  t,
  profile,
  bindingEvents,
  camera,
  views,
  validation,
  validating,
  onMode,
  onName,
}: {
  t: Translate;
  profile: AttentionProfile;
  bindingEvents: AttentionBindingCatalogItem[];
  camera: AttentionCameraCatalogItem | null;
  views: AttentionViewCatalogItem[];
  validation: AttentionValidationResponse | null;
  validating: boolean;
  onMode: (mode: AttentionExecutionMode) => void;
  onName: (name: string) => void;
}): React.ReactElement {
  const viewLabel = (id: string) => views.find((view) => view.id === id)?.label || id;
  const paused = profile.mode === "paused";
  const selectedMode = paused ? profile.resume_mode ?? "disabled" : profile.mode;
  return (
    <section>
      <h3 className="ptzAttentionCardTitle">{t("ext.ptz_attention.wizard.safety.title", {}, "Review safety and execution mode")}</h3>
      <div className="ptzAttentionMuted">{t("ext.ptz_attention.wizard.safety.desc", {}, "Shadow mode records complete decisions with zero motor commands.")}</div>
      <label className="ptzAttentionField" style={{ marginTop: 12 }}>
        <span>{t("ext.ptz_attention.wizard.name", {}, "Profile name")}</span>
        <input className="input" value={profile.name} onChange={(event) => onName(event.target.value)} autoComplete="off" />
      </label>
      {paused ? (
        <div className="ptzAttentionNotice" data-tone="warning" style={{ marginTop: 12 }}>
          {t("ext.ptz_attention.wizard.mode.paused", {}, "Resume the profile from the cockpit before changing its execution mode.")}
        </div>
      ) : null}
      <fieldset className="ptzAttentionModeFieldset">
        <legend>{t("ext.ptz_attention.wizard.mode", {}, "Execution mode")}</legend>
        <div className="ptzAttentionChoiceGrid">
        <label className="ptzAttentionChoice" data-selected={selectedMode === "shadow"}>
          <div><input type="radio" name="attention-mode" disabled={paused} checked={selectedMode === "shadow"} onChange={() => onMode("shadow")} /><span className="ptzAttentionChoiceTitle">{t("ext.ptz_attention.wizard.mode.shadow.title", {}, "Shadow review")}</span></div>
          <div className="ptzAttentionChoiceMeta">{t("ext.ptz_attention.wizard.mode.shadow.desc", {}, "Evaluate without moving the camera.")}</div>
        </label>
        <label className="ptzAttentionChoice" data-selected={selectedMode === "live_preset"}>
          <div><input type="radio" name="attention-mode" disabled={paused} checked={selectedMode === "live_preset"} onChange={() => onMode("live_preset")} /><span className="ptzAttentionChoiceTitle">{t("ext.ptz_attention.wizard.mode.live.title", {}, "Live movement")}</span></div>
          <div className="ptzAttentionChoiceMeta">{t("ext.ptz_attention.wizard.mode.live.desc", {}, "Move only among selected calibrated views.")}</div>
        </label>
        <label className="ptzAttentionChoice" data-selected={selectedMode === "disabled"}>
          <div><input type="radio" name="attention-mode" disabled={paused} checked={selectedMode === "disabled"} onChange={() => onMode("disabled")} /><span className="ptzAttentionChoiceTitle">{t("ext.ptz_attention.wizard.mode.disabled.title", {}, "Disabled")}</span></div>
          <div className="ptzAttentionChoiceMeta">{t("ext.ptz_attention.wizard.mode.disabled.desc", {}, "Keep the profile configured without accepting events.")}</div>
        </label>
        </div>
      </fieldset>
      <div className="ptzAttentionMetrics">
        <SummaryMetric label={t("ext.ptz_attention.wizard.summary.events", {}, "Event types")} value={profile.event_policies.filter((policy) => policy.enabled).map((policy) => policy.event_type).join(", ") || "-"} />
        <SummaryMetric label={t("ext.ptz_attention.wizard.summary.camera", {}, "Camera")} value={camera?.name || profile.camera_id || "-"} />
        <SummaryMetric label={t("ext.ptz_attention.wizard.summary.home", {}, "Home")} value={viewLabel(profile.home_view_id) || "-"} />
        <SummaryMetric label={t("ext.ptz_attention.wizard.summary.views", {}, "Focus views")} value={profile.eligible_view_ids.map(viewLabel).join(", ") || "-"} />
        <SummaryMetric label={t("ext.ptz_attention.wizard.mode", {}, "Execution mode")} value={t(`ext.ptz_attention.profile.mode.${profile.mode}`, {}, profile.mode)} />
        <SummaryMetric label={t("ext.ptz_attention.wizard.summary.hard_timeout", {}, "Hard focus timeout")} value={`${profile.max_focus_seconds} ${t("ext.ptz_attention.wizard.policy.seconds", {}, "seconds")}`} />
        <SummaryMetric
          label={t("ext.ptz_attention.wizard.summary.binding", {}, "Pipeline binding")}
          value={bindingEvents.some((binding) => binding.enabled)
            ? t("ext.ptz_attention.wizard.events.bound", {}, "Bound")
            : t("ext.ptz_attention.wizard.events.unbound", {}, "Binding pending")}
        />
      </div>
      <h4 className="ptzAttentionCardTitle" style={{ marginTop: 18 }}>{t("ext.ptz_attention.wizard.validation.title", {}, "Readiness check")}</h4>
      {validating ? <div className="ptzAttentionMuted">{t("ext.ptz_attention.loading", {}, "Loading...")}</div> : null}
      {!validating && validation?.issues?.length ? (
        <div className="ptzAttentionIssueList">
          {validation.issues.map((issue, index) => (
            <div className="ptzAttentionNotice" data-tone={issue.severity === "error" ? "danger" : issue.severity} key={`${issue.code}-${index}`}>
              <strong>{readinessIssueLabel(issue.code, t)}</strong>{issue.message ? ` · ${issue.message}` : ""}
            </div>
          ))}
        </div>
      ) : null}
      {!validating && validation?.ok && validation.issues.length === 0 ? (
        <div className="ptzAttentionNotice" data-tone="success" style={{ marginTop: 10 }}>
          {t(
            "ext.ptz_attention.wizard.validation.ready",
            {
              mode: t(
                `ext.ptz_attention.profile.mode.${profile.mode === "paused" ? profile.resume_mode ?? "disabled" : profile.mode}`,
                {},
                profile.mode,
              ),
            },
            "Profile is ready.",
          )}
        </div>
      ) : null}
    </section>
  );
}

function SummaryMetric({ label, value }: { label: string; value: string }): React.ReactElement {
  return <div className="ptzAttentionMetric"><div className="ptzAttentionSubtle">{label}</div><div className="ptzAttentionMetricValue">{value}</div></div>;
}

function cloneProfile(profile: AttentionProfile): AttentionProfile {
  return {
    ...profile,
    eligible_view_ids: [...profile.eligible_view_ids],
    event_policies: profile.event_policies.map((policy) => ({ ...policy })),
  };
}

function uniqueCompositions(views: AttentionViewCatalogItem[]): Array<{ id: string; name: string }> {
  const result = new Map<string, string>();
  for (const view of views) {
    if (!view.composition_id) continue;
    result.set(view.composition_id, view.composition_name || view.composition_id);
  }
  return Array.from(result, ([id, name]) => ({ id, name }));
}

function isReadyView(view: AttentionViewCatalogItem): boolean {
  return view.quality === "ready" && view.pose_bound !== false;
}

function uniqueReadyViews(views: AttentionViewCatalogItem[]): AttentionViewCatalogItem[] {
  return views.filter(
    (view) =>
      isReadyView(view) &&
      views.filter(
        (candidate) => candidate.composition_id === view.composition_id && candidate.id === view.id,
      ).length === 1,
  );
}

function viewSupportsSource(
  view: AttentionViewCatalogItem,
  camera: AttentionCameraCatalogItem | null,
  sourceId: string,
): boolean {
  const source = camera?.sources.find((item) => item.id === sourceId) ?? null;
  if (!source || !source.enabled || source.kind !== "video" || !source.has_ptz) return false;
  if (view.physical_view_id && view.physical_view_id !== source.view_id) return false;
  if (view.compatible_source_ids.length > 0 && !view.compatible_source_ids.includes(source.id)) return false;
  if (view.compatible_roles.length > 0 && !view.compatible_roles.includes(source.role)) return false;
  if (view.compatible_source_ids.length === 0 && view.compatible_roles.length === 0) {
    return source.role === "main" || source.role === "sub";
  }
  return true;
}

function viewQualityLabel(view: AttentionViewCatalogItem, t: Translate): string {
  if (view.quality === "ready") return t("ext.ptz_attention.wizard.view.ready", {}, "Ready");
  if (view.quality === "review" || view.quality === "estimated") return t("ext.ptz_attention.wizard.view.review", {}, "Needs review");
  return t("ext.ptz_attention.wizard.view.incomplete", {}, "Incomplete");
}

function readinessIssueLabel(code: string, t: Translate): string {
  const normalized = String(code || "").trim().toLowerCase();
  return t(`ext.ptz_attention.issue.${normalized}`, {}, normalized.replace(/_/g, " ") || "-");
}

function validateStep(
  step: number,
  profile: AttentionProfile,
  context: {
    selectedCamera: AttentionCameraCatalogItem | null;
    readyViews: AttentionViewCatalogItem[];
    sharedObserver: boolean;
    sharedObserverAcknowledged: boolean;
    t: Translate;
  },
): string | null {
  if (step === 0 && profile.event_policies.filter((policy) => policy.enabled).length === 0) {
    return context.t("ext.ptz_attention.wizard.error.events", {}, "Select at least one compatible event source.");
  }
  if (step === 1) {
    if (!profile.camera_id || !profile.ptz_device_id) return context.t("ext.ptz_attention.wizard.error.camera", {}, "Select a PTZ camera.");
    if (!isEnabledVideoSource(context.selectedCamera, profile.source_id)) {
      return context.t("ext.ptz_attention.wizard.error.source", {}, "Select a video source.");
    }
    if (!profile.home_view_id || !context.readyViews.some((view) => view.id === profile.home_view_id)) {
      return context.t("ext.ptz_attention.wizard.error.home", {}, "Select a ready home view.");
    }
    if (context.sharedObserver && !context.sharedObserverAcknowledged) {
      return context.t("ext.ptz_attention.wizard.error.shared_ack", {}, "Acknowledge the shared observer camera.");
    }
  }
  if (step === 2) {
    if (profile.max_focus_seconds <= profile.min_focus_seconds) {
      return context.t("ext.ptz_attention.wizard.error.maximum_focus", {}, "Hard focus timeout must be greater than minimum focus.");
    }
    if (profile.candidate_confirm_seconds >= profile.stale_timeout_seconds) {
      return context.t("ext.ptz_attention.wizard.error.stale", {}, "Event stale timeout must be greater than candidate confirmation.");
    }
  }
  if (step === 3) {
    if (!profile.name.trim()) return context.t("ext.ptz_attention.wizard.error.name", {}, "Name this profile.");
    const effectiveMode = profile.mode === "paused" ? profile.resume_mode : profile.mode;
    if (effectiveMode === "live_preset" && !hasDistinctReadyEventView(profile, context.readyViews)) {
      return context.t(
        "ext.ptz_attention.wizard.error.distinct_view",
        {},
        "Select at least one ready focus view distinct from home before enabling live movement.",
      );
    }
  }
  return null;
}

function isEnabledVideoSource(camera: AttentionCameraCatalogItem | null, sourceId: string): boolean {
  return Boolean(camera?.sources.some(
    (source) => source.id === sourceId && source.enabled && source.kind === "video" && source.has_ptz,
  ));
}

function hasDistinctReadyEventView(profile: AttentionProfile, readyViews: AttentionViewCatalogItem[]): boolean {
  const readyIds = new Set(readyViews.map((view) => view.id));
  return profile.eligible_view_ids.some(
    (viewId) => viewId !== profile.home_view_id && readyIds.has(viewId),
  );
}

function canValidateProfile(
  profile: AttentionProfile,
  camera: AttentionCameraCatalogItem | null,
  readyViews: AttentionViewCatalogItem[],
  sharedObserver: boolean,
  sharedAcknowledged: boolean,
): boolean {
  const effectiveMode = profile.mode === "paused" ? profile.resume_mode : profile.mode;
  return Boolean(
    profile.name.trim() &&
    profile.event_policies.some((policy) => policy.enabled) &&
    profile.camera_id &&
    isEnabledVideoSource(camera, profile.source_id) &&
    readyViews.some((view) => view.id === profile.home_view_id) &&
    (effectiveMode !== "live_preset" || hasDistinctReadyEventView(profile, readyViews)) &&
    (!sharedObserver || sharedAcknowledged),
  );
}

function profileIdFromName(name: string): string {
  const normalized = name
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^[_\d-]+|_+$/g, "")
    .slice(0, 64);
  const base = normalized || "attention";
  return base.length < 2 ? `${base}_profile` : base;
}

function clampNumber(value: string, fallback: number, min: number, max: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}
