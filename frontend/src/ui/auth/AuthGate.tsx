import React, { useCallback, useEffect, useMemo, useState } from "react";

import { App } from "../App";
import {
  getAuthStatus,
  login,
  logout,
  setupOwner,
  type AuthStatus,
} from "../../util/api";

type AuthStage = "loading" | "ready" | "error";

function defaultDeviceLabel(): string {
  const ua = typeof navigator === "undefined" ? "browser" : navigator.userAgent;
  return ua.slice(0, 72) || "browser";
}

export function AuthGate(): React.ReactElement {
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
        setError("Passwords do not match");
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
    if (status?.requires_setup) return "Create local owner account";
    return "Sign in to Toposync";
  }, [status]);

  if (stage === "loading") {
    return (
      <div className="authRoot">
        <div className="authCard">
          <div className="authTitle">Toposync</div>
          <div className="authSubtitle">Loading authentication state...</div>
        </div>
      </div>
    );
  }

  if (stage === "error") {
    return (
      <div className="authRoot">
        <div className="authCard">
          <div className="authTitle">Toposync</div>
          <div className="authSubtitle">Failed to reach backend authentication.</div>
          {error ? <div className="errorText">{error}</div> : null}
          <button className="primaryButton" type="button" onClick={() => void loadStatus()}>
            Retry
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
          <div className="authSubtitle">Missing auth status.</div>
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
              Username
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
              Display name
            </label>
            <input
              id="setupDisplayName"
              className="input"
              value={setupDisplayName}
              onChange={(e) => setSetupDisplayName(e.target.value)}
              autoComplete="name"
            />

            <label className="authLabel" htmlFor="setupPassword">
              Password
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
              Confirm password
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
              {setupBusy ? "Creating..." : "Create owner"}
            </button>
          </>
        ) : (
          <>
            <label className="authLabel" htmlFor="loginUsername">
              Username
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
              Password
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
              {loginBusy ? "Signing in..." : "Sign in"}
            </button>
          </>
        )}

        {error ? <div className="errorText">{error}</div> : null}
      </form>
    </div>
  );
}
