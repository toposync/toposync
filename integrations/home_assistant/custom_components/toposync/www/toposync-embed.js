const DEFAULT_CARD_CONFIG = {
  title: "Toposync",
  path: "/",
  height: "720px",
  show_header: true,
  allow_fullscreen: true,
  open_in_new_tab: true,
};

function normalizePath(value) {
  let path = String(value || "/").trim() || "/";
  if (!path.startsWith("/")) path = `/${path}`;
  if (path.startsWith("//") || path.includes("://")) return "/";
  return path;
}

function normalizeHeight(value, fallback) {
  const height = String(value || "").trim();
  return height || fallback;
}

function isLoopbackHost(value) {
  const host = String(value || "").toLowerCase();
  return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
}

function alignLoopbackUrlWithHomeAssistant(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    if (isLoopbackHost(parsed.hostname) && isLoopbackHost(window.location.hostname)) {
      parsed.hostname = window.location.hostname;
    }
    return parsed.toString();
  } catch {
    return raw;
  }
}

function callToposyncEmbedConfig(hass, config) {
  const payload = {
    type: "toposync/embed_config",
    path: normalizePath(config.path),
  };
  if (config.entry_id) payload.entry_id = String(config.entry_id);
  if (typeof hass.callWS === "function") return hass.callWS(payload);
  return hass.connection.sendMessagePromise(payload);
}

class ToposyncEmbedBase extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...DEFAULT_CARD_CONFIG };
    this._hass = null;
    this._state = {
      loading: true,
      connected: false,
      embed_url: "",
      base_url: "",
      warnings: [],
      auth_mode: "",
    };
    this._requestedKey = "";
  }

  setConfig(config) {
    this._config = {
      ...DEFAULT_CARD_CONFIG,
      ...(config || {}),
      path: normalizePath((config || {}).path),
      height: normalizeHeight((config || {}).height, DEFAULT_CARD_CONFIG.height),
      show_header: (config || {}).show_header !== false,
      allow_fullscreen: (config || {}).allow_fullscreen !== false,
      open_in_new_tab: (config || {}).open_in_new_tab !== false,
    };
    this._requestedKey = "";
    this._load();
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._load();
  }

  _load() {
    if (!this._hass || !this._config) return;
    const key = `${this._config.entry_id || ""}:${this._config.path || "/"}`;
    if (this._requestedKey === key) return;
    this._requestedKey = key;
    this._state = { ...this._state, loading: true, warnings: [] };
    this._render();
    callToposyncEmbedConfig(this._hass, this._config)
      .then((result) => {
        this._state = {
          loading: false,
          connected: result.connected === true,
          embed_url: alignLoopbackUrlWithHomeAssistant(result.embed_url),
          base_url: alignLoopbackUrlWithHomeAssistant(result.base_url),
          warnings: Array.isArray(result.warnings) ? result.warnings.map(String) : [],
          auth_mode: String(result.auth_mode || ""),
        };
        this._render();
      })
      .catch((error) => {
        this._state = {
          loading: false,
          connected: false,
          embed_url: "",
          base_url: "",
          warnings: [error && error.message ? String(error.message) : "Failed to load Toposync."],
          auth_mode: "error",
        };
        this._render();
      });
  }

  _openToposync() {
    const target = this._state.embed_url || this._state.base_url;
    if (target) window.open(target, "_blank", "noopener,noreferrer");
  }

  _styles() {
    return `
      :host {
        display: block;
        color: var(--primary-text-color);
      }
      .toposync-frame-wrap {
        position: relative;
        width: 100%;
        min-height: 240px;
        background: var(--card-background-color, #fff);
        overflow: hidden;
      }
      .toposync-frame {
        display: block;
        width: 100%;
        height: 100%;
        border: 0;
        background: #fff;
      }
      .toposync-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 16px;
        border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
      }
      .toposync-title {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: 16px;
        font-weight: 500;
      }
      .toposync-actions {
        display: flex;
        align-items: center;
        gap: 8px;
        flex: 0 0 auto;
      }
      .toposync-button {
        border: 0;
        border-radius: 4px;
        padding: 7px 10px;
        background: var(--primary-color);
        color: var(--text-primary-color, #fff);
        font: inherit;
        cursor: pointer;
      }
      .toposync-button.secondary {
        background: transparent;
        color: var(--primary-color);
      }
      .toposync-message {
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 10px;
        min-height: 220px;
        padding: 20px;
        box-sizing: border-box;
        color: var(--secondary-text-color);
      }
      .toposync-message strong {
        color: var(--primary-text-color);
        font-size: 15px;
      }
      .toposync-warning {
        font-size: 13px;
        line-height: 1.4;
      }
    `;
  }

  _renderContent(height) {
    if (this._state.loading) {
      return `
        <div class="toposync-message" style="height:${height}">
          <strong>Loading Toposync...</strong>
        </div>
      `;
    }
    if (!this._state.embed_url) {
      const warning = this._state.warnings[0] || "Toposync is not configured.";
      return `
        <div class="toposync-message" style="height:${height}">
          <strong>Toposync is unavailable</strong>
          <div class="toposync-warning">${this._escape(warning)}</div>
        </div>
      `;
    }
    const allowFullscreen = this._config.allow_fullscreen ? "allowfullscreen" : "";
    return `
      <div class="toposync-frame-wrap" style="height:${height}">
        <iframe
          class="toposync-frame"
          src="${this._escapeAttr(this._state.embed_url)}"
          title="Toposync"
          allow="fullscreen; autoplay; camera; microphone; clipboard-read; clipboard-write"
          ${allowFullscreen}
        ></iframe>
      </div>
    `;
  }

  _header() {
    if (!this._config.show_header) return "";
    const openButton = this._config.open_in_new_tab
      ? `<button class="toposync-button secondary" type="button" data-action="open">Open</button>`
      : "";
    return `
      <div class="toposync-header">
        <div class="toposync-title">${this._escape(this._config.title || "Toposync")}</div>
        <div class="toposync-actions">${openButton}</div>
      </div>
    `;
  }

  _escape(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  _escapeAttr(value) {
    return this._escape(value).replace(/"/g, "&quot;");
  }
}

class ToposyncEmbedCard extends ToposyncEmbedBase {
  static getConfigElement() {
    return document.createElement("hui-generic-entity-row");
  }

  static getStubConfig() {
    return { ...DEFAULT_CARD_CONFIG };
  }

  getCardSize() {
    return 8;
  }

  _render() {
    const height = normalizeHeight(this._config.height, DEFAULT_CARD_CONFIG.height);
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        ${this._header()}
        ${this._renderContent(height)}
      </ha-card>
    `;
    const button = this.shadowRoot.querySelector('[data-action="open"]');
    if (button) button.addEventListener("click", () => this._openToposync());
  }
}

class ToposyncPanel extends ToposyncEmbedBase {
  set panel(panel) {
    const config = (panel && panel.config) || {};
    this.setConfig({
      title: "Toposync",
      path: config.path || "/",
      height: config.height || "100%",
      show_header: config.show_header === true,
      allow_fullscreen: true,
      open_in_new_tab: true,
      entry_id: config.entry_id,
    });
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        ${this._styles()}
        :host {
          display: block;
          height: 100%;
        }
        .panel-root {
          height: calc(100vh - var(--header-height, 56px));
          min-height: 420px;
          background: var(--card-background-color, #fff);
        }
      </style>
      <div class="panel-root">
        ${this._renderContent("100%")}
      </div>
    `;
  }
}

if (!customElements.get("toposync-embed-card")) {
  customElements.define("toposync-embed-card", ToposyncEmbedCard);
}
if (!customElements.get("toposync-panel")) {
  customElements.define("toposync-panel", ToposyncPanel);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "toposync-embed-card")) {
  window.customCards.push({
    type: "toposync-embed-card",
    name: "Toposync",
    description: "Embed Toposync in a Home Assistant dashboard.",
  });
}
