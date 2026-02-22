import React, { useCallback, useEffect, useMemo, useState } from "react";

import { App } from "../App";
import {
  getAuthStatus,
  login,
  logout,
  setupOwner,
  type AuthStatus,
} from "../../util/api";
import { i18n } from "../../util/i18n";

type AuthStage = "loading" | "ready" | "error";

function defaultDeviceLabel(): string {
  const ua = typeof navigator === "undefined" ? "browser" : navigator.userAgent;
  return ua.slice(0, 72) || "browser";
}

export function AuthGate(): React.ReactElement {
  const { t } = i18n.useI18n();
  const [stage, setStage] = useState<AuthStage>("loading");
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string>("");

  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);

  const [setupUsername, setSetupUsername] = useState("owner");
  const [setupDisplayName, setSetupDisplayName] = useState("Owner");
  const [setupPassword, setSetupPassword] = useState("");
  const [setupConfirm, setSetupConfirm] = useState("");
  const [setupBusy, setSetupBusy] = useState(false);

  const loadStatus = useCallback(async () => {
    setStage("loading");
    setError("");
    try {
      const next = await getAuthStatus();
      setStatus(next);
      setStage("ready");
    } catch (err) {
      setStage("error");
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const onLogin = useCallback(
    async (ev: React.FormEvent) => {
      ev.preventDefault();
      if (loginBusy) return;
      setLoginBusy(true);
      setError("");
      try {
        await login({
          username: loginUsername.trim(),
          password: loginPassword,
          device_label: defaultDeviceLabel(),
        });
        setLoginPassword("");
        await loadStatus();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoginBusy(false);
      }
    },
    [loadStatus, loginBusy, loginPassword, loginUsername],
  );

  const onSetup = useCallback(
    async (ev: React.FormEvent) => {
      ev.preventDefault();
      if (setupBusy) return;
      if (setupPassword !== setupConfirm) {
        setError(t("core.ui.auth.error.password_mismatch"));
        return;
      }
      setSetupBusy(true);
      setError("");
      try {
        await setupOwner({
          username: setupUsername.trim(),
          display_name: setupDisplayName.trim(),
          password: setupPassword,
          device_label: defaultDeviceLabel(),
        });
        setSetupPassword("");
        setSetupConfirm("");
        await loadStatus();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSetupBusy(false);
      }
    },
    [loadStatus, setupBusy, setupConfirm, setupDisplayName, setupPassword, setupUsername],
  );

  const handleLogout = useCallback(async () => {
    await logout();
    await loadStatus();
  }, [loadStatus]);

  const title = useMemo(() => {
    if (status?.requires_setup) return t("core.ui.auth.setup.title");
    return t("core.ui.auth.login.title");
  }, [status]);

  if (stage === "loading") {
    return (
      <div className="authRoot">
        <div className="authCard">
          <div className="authTitle">Toposync</div>
          <div className="authSubtitle">{t("core.ui.auth.loading_state")}</div>
        </div>
      </div>
    );
  }

  if (stage === "error") {
    return (
      <div className="authRoot">
        <div className="authCard">
          <div className="authTitle">Toposync</div>
          <div className="authSubtitle">{t("core.ui.auth.backend_unreachable")}</div>
          {error ? <div className="errorText">{error}</div> : null}
          <button className="primaryButton" type="button" onClick={() => void loadStatus()}>
            {t("core.actions.retry")}
          </button>
        </div>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="authRoot">
        <div className="authCard">
          <div className="authTitle">Toposync</div>
          <div className="authSubtitle">{t("core.ui.auth.missing_status")}</div>
        </div>
      </div>
    );
  }

  if (status.mode === "bypass" || status.authenticated) {
    return <App authUser={status.user} authMode={status.mode} onLogout={handleLogout} />;
  }

  return (
    <div className="authRoot">
      <form className="authCard" onSubmit={status.requires_setup ? onSetup : onLogin}>
        <div className="authTitle">Toposync</div>
        <div className="authSubtitle">{title}</div>

        {status.requires_setup ? (
          <>
            <label className="authLabel" htmlFor="setupUsername">
              {t("core.ui.auth.field.username")}
            </label>
            <input
              id="setupUsername"
              className="input"
              value={setupUsername}
              onChange={(e) => setSetupUsername(e.target.value)}
              autoComplete="username"
              required
            />

            <label className="authLabel" htmlFor="setupDisplayName">
              {t("core.ui.auth.field.display_name")}
            </label>
            <input
              id="setupDisplayName"
              className="input"
              value={setupDisplayName}
              onChange={(e) => setSetupDisplayName(e.target.value)}
              autoComplete="name"
            />

            <label className="authLabel" htmlFor="setupPassword">
              {t("core.ui.auth.field.password")}
            </label>
            <input
              id="setupPassword"
              className="input"
              type="password"
              value={setupPassword}
              onChange={(e) => setSetupPassword(e.target.value)}
              autoComplete="new-password"
              minLength={8}
              required
            />

            <label className="authLabel" htmlFor="setupConfirm">
              {t("core.ui.auth.field.confirm_password")}
            </label>
            <input
              id="setupConfirm"
              className="input"
              type="password"
              value={setupConfirm}
              onChange={(e) => setSetupConfirm(e.target.value)}
              autoComplete="new-password"
              minLength={8}
              required
            />

            <button className="primaryButton" type="submit" disabled={setupBusy}>
              {setupBusy ? t("core.ui.auth.action.creating") : t("core.ui.auth.action.create_owner")}
            </button>
          </>
        ) : (
          <>
            <label className="authLabel" htmlFor="loginUsername">
              {t("core.ui.auth.field.username")}
            </label>
            <input
              id="loginUsername"
              className="input"
              value={loginUsername}
              onChange={(e) => setLoginUsername(e.target.value)}
              autoComplete="username"
              required
            />

            <label className="authLabel" htmlFor="loginPassword">
              {t("core.ui.auth.field.password")}
            </label>
            <input
              id="loginPassword"
              className="input"
              type="password"
              value={loginPassword}
              onChange={(e) => setLoginPassword(e.target.value)}
              autoComplete="current-password"
              required
            />

            <button className="primaryButton" type="submit" disabled={loginBusy}>
              {loginBusy ? t("core.ui.auth.action.signing_in") : t("core.ui.auth.action.sign_in")}
            </button>
          </>
        )}

        {error ? <div className="errorText">{error}</div> : null}
      </form>
    </div>
  );
}
