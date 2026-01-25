import { useSyncExternalStore } from "react";

export type Locale = "en" | "pt-BR";

export type Translations = Record<string, string>;
export type TranslationBundle = Partial<Record<Locale, Translations>>;

export type LocalizedString =
  | string
  | {
      key: string;
      params?: Record<string, unknown>;
      fallback?: string;
    };

type I18nApi = {
  getLocale: () => Locale;
  setLocale: (locale: Locale) => void;
  subscribe: (listener: () => void) => () => void;
  registerTranslations: (bundle: TranslationBundle) => void;
  t: (key: string, params?: Record<string, unknown>, fallback?: string) => string;
  useI18n: () => { locale: Locale; t: I18nApi["t"]; setLocale: I18nApi["setLocale"] };
};

const STORAGE_KEY = "toposync.locale";

const translationsByLocale: Record<Locale, Translations> = {
  en: {
    "core.actions.add": "Add",
    "core.actions.apply": "Apply",
    "core.actions.back": "Back",
    "core.actions.cancel": "Cancel",
    "core.actions.close": "Close",
    "core.actions.delete": "Delete",
    "core.actions.edit": "Edit",
    "core.actions.rename": "Rename",
    "core.actions.save": "Save",

    "core.ui.rendering": "Rendering",
    "core.ui.composition": "Composition",
    "core.ui.notifications": "Notifications",
    "core.ui.notifications.aria_open": "Open notifications",
    "core.ui.notifications.aria_close": "Close notifications",
    "core.ui.layers": "Layers",
    "core.ui.add": "Add",
    "core.ui.tools": "Tools",
    "core.ui.action": "Action",
    "core.ui.view_settings.title": "View",
    "core.ui.view_settings.aria": "View settings",
    "core.ui.view_settings.wall_height": "Wall height",
    "core.ui.view_settings.interactivity": "Interactivity",
    "core.ui.view_settings.ghost_walls": "Ghost walls",
    "core.ui.view_settings.ghost_walls_desc": "Make walls semi-transparent and allow clicking elements through them.",
    "core.ui.view_settings.graphics_quality": "Graphics",
    "core.ui.graphics_quality.simplified": "Simplified",
    "core.ui.graphics_quality.simplified_desc": "Better performance. Lighter effects.",
    "core.ui.graphics_quality.detailed": "Detailed",
    "core.ui.graphics_quality.detailed_desc": "More detail and effects. Higher GPU/CPU usage.",
    "core.ui.settings.title": "Settings",
    "core.ui.settings.aria": "Settings",
    "core.ui.settings.sections.view": "View options",
    "core.ui.settings.sections.view_desc": "Walls, interactivity and graphics.",
    "core.ui.settings.sections.core": "Core",
    "core.ui.settings.sections.core_desc": "Language and general preferences.",
    "core.ui.settings.no_extensions": "No extension settings yet.",
    "core.ui.settings.backend_offline_title": "Backend offline",
    "core.ui.settings.backend_offline_desc": "Settings changes won't be persisted until the backend is running.",
    "core.ui.settings.language": "Language",
    "core.ui.settings.language.pt": "Português (Brasil)",
    "core.ui.settings.language.pt_desc": "Portuguese interface.",
    "core.ui.settings.language.en": "English",
    "core.ui.settings.language.en_desc": "English interface.",
    "core.ui.settings.theme": "Theme",
    "core.ui.settings.theme.default": "Default",
    "core.ui.settings.theme.default_desc": "Toposync default theme.",
    "core.ui.settings.save_changes": "Save changes",
    "core.ui.settings.save_all_changes": "Save all changes",
    "core.ui.settings.discard_changes": "Discard changes",
    "core.ui.settings.discard_and_close": "Discard and close",
    "core.ui.settings.changes_saved": "Saved",
    "core.ui.settings.unsaved_changes": "Unsaved changes",
    "core.ui.settings.unsaved_changes_in": "Unsaved: {{sections}}",
    "core.ui.settings.saving": "Saving…",
    "core.ui.settings.confirm_discard_title": "Discard changes?",
    "core.ui.settings.confirm_discard_desc": "Discard all your pending changes in Settings?",
    "core.ui.settings.confirm_close_title": "Discard and close?",
    "core.ui.settings.confirm_close_desc": "You have unsaved changes. Discard them and close Settings?",

    "core.ui.wall_height.low": "Low",
    "core.ui.wall_height.low_desc": "Low walls for quick overview.",
    "core.ui.wall_height.medium": "Medium",
    "core.ui.wall_height.medium_desc": "Medium height for planning.",
    "core.ui.wall_height.high": "High",
    "core.ui.wall_height.high_desc": "Full height (typical wall).",

    "core.ui.layers_group_walls": "Walls",
    "core.ui.layers_group_areas": "Areas",
    "core.ui.layers_group_background": "Background",
    "core.ui.layers.hide": "Hide layer",
    "core.ui.layers.show": "Show layer",
    "core.ui.layers.lock": "Lock movement",
    "core.ui.layers.unlock": "Unlock movement",
    "core.ui.layers.reorder": "Reorder layer",

    "core.tools.navigate": "Navigate",
    "core.tools.navigate_desc": "Pan around the canvas.",
    "core.tools.select": "Select",
    "core.tools.select_desc": "Select and move elements.",

    "core.ui.empty_title": "Nothing configured yet",
    "core.ui.empty_desc": "Click “Edit” to add elements to the composition.",
    "core.ui.notifications_empty": "No notifications yet.",
    "core.ui.image_preview": "Image",
    "core.ui.loading": "Loading…",
    "core.ui.error": "Error",
    "core.ui.element_types_empty": "No extensions have registered elements yet.",
    "core.ui.layers_empty": "No elements added yet.",

    "core.ui.render_modal.title": "Rendering",
    "core.ui.render_modal.option_3d.title": "3D (ThreeJS)",
    "core.ui.render_modal.option_3d.desc": "Interactive 3D view.",
    "core.ui.render_modal.option_2d.title": "2D (Snapshot)",
    "core.ui.render_modal.option_2d.desc": "Top-down snapshot with Home Assistant overlays.",

    "core.ui.main2d.cluster.title": "Multiple items ({{count}})",
    "core.ui.main2d.cluster.tooltip": "{{count}} items",

    "core.ui.action_unavailable": "No actions available for this element.",

    "core.compositions.modal.title": "Compositions",
    "core.compositions.section.new": "New composition",
    "core.compositions.section.list": "Your compositions",
    "core.compositions.new.placeholder": "Name (e.g. Ground, Upstairs...)",
    "core.compositions.delete_confirm": "Delete “{{name}}”?",
    "core.compositions.cannot_delete_last": "You can’t delete the last composition",
    "core.compositions.aria.create": "Create composition",
    "core.compositions.aria.save_name": "Save name",
    "core.compositions.aria.cancel": "Cancel",
    "core.compositions.aria.rename": "Rename composition",
    "core.compositions.aria.delete": "Delete composition",
    "core.compositions.aria.cancel_delete": "Cancel delete",
    "core.compositions.aria.confirm_delete": "Confirm delete",
    "core.compositions.error.create": "Failed to create composition",
    "core.compositions.error.activate": "Failed to switch composition",
    "core.compositions.error.rename": "Failed to rename composition",
    "core.compositions.error.delete": "Failed to delete composition",

    "core.element_editor.title": "Edit element",
    "core.element_editor.name": "Name",
    "core.element_editor.pos_x": "Position X",
    "core.element_editor.pos_y": "Position Y",
    "core.element_editor.pos_z": "Position Z",
    "core.element_editor.rot_x": "Rotation X (degrees)",
    "core.element_editor.rot_y": "Rotation Y (degrees)",
    "core.element_editor.rot_z": "Rotation Z (degrees)",
    "core.element_editor.delete": "Delete element",

    "core.modal.aria.close": "Close",
  },
  "pt-BR": {
    "core.actions.add": "Adicionar",
    "core.actions.apply": "Aplicar",
    "core.actions.back": "Voltar",
    "core.actions.cancel": "Cancelar",
    "core.actions.close": "Fechar",
    "core.actions.delete": "Excluir",
    "core.actions.edit": "Editar",
    "core.actions.rename": "Renomear",
    "core.actions.save": "Salvar",

    "core.ui.rendering": "Renderização",
    "core.ui.composition": "Composição",
    "core.ui.notifications": "Notificações",
    "core.ui.notifications.aria_open": "Abrir notificações",
    "core.ui.notifications.aria_close": "Fechar notificações",
    "core.ui.layers": "Camadas",
    "core.ui.add": "Adicionar",
    "core.ui.tools": "Ferramentas",
    "core.ui.action": "Ação",
    "core.ui.view_settings.title": "Visualização",
    "core.ui.view_settings.aria": "Configurações de visualização",
    "core.ui.view_settings.wall_height": "Altura da parede",
    "core.ui.view_settings.interactivity": "Interatividade",
    "core.ui.view_settings.ghost_walls": "Paredes transparentes",
    "core.ui.view_settings.ghost_walls_desc": "Deixa as paredes semi-transparentes e permite clicar nos elementos através delas.",
    "core.ui.view_settings.graphics_quality": "Gráficos",
    "core.ui.graphics_quality.simplified": "Simplificados",
    "core.ui.graphics_quality.simplified_desc": "Mais leve e com melhor performance.",
    "core.ui.graphics_quality.detailed": "Detalhados",
    "core.ui.graphics_quality.detailed_desc": "Mais detalhes e efeitos. Exige mais do computador.",
    "core.ui.settings.title": "Configurações",
    "core.ui.settings.aria": "Configurações",
    "core.ui.settings.sections.view": "Opções de visualização",
    "core.ui.settings.sections.view_desc": "Paredes, interatividade e gráficos.",
    "core.ui.settings.sections.core": "Base",
    "core.ui.settings.sections.core_desc": "Idioma e preferências gerais.",
    "core.ui.settings.no_extensions": "Nenhuma extensão adicionou configurações ainda.",
    "core.ui.settings.backend_offline_title": "Backend indisponível",
    "core.ui.settings.backend_offline_desc": "As alterações não serão persistidas até o backend estar rodando.",
    "core.ui.settings.language": "Idioma",
    "core.ui.settings.language.pt": "Português (Brasil)",
    "core.ui.settings.language.pt_desc": "Interface em português.",
    "core.ui.settings.language.en": "English",
    "core.ui.settings.language.en_desc": "Interface in English.",
    "core.ui.settings.theme": "Tema",
    "core.ui.settings.theme.default": "Padrão",
    "core.ui.settings.theme.default_desc": "Tema padrão do Toposync.",
    "core.ui.settings.save_changes": "Salvar alterações",
    "core.ui.settings.save_all_changes": "Salvar todas as alterações",
    "core.ui.settings.discard_changes": "Descartar alterações",
    "core.ui.settings.discard_and_close": "Descartar e fechar",
    "core.ui.settings.changes_saved": "Salvo",
    "core.ui.settings.unsaved_changes": "Alterações não salvas",
    "core.ui.settings.unsaved_changes_in": "Não salvo: {{sections}}",
    "core.ui.settings.saving": "Salvando…",
    "core.ui.settings.confirm_discard_title": "Descartar alterações?",
    "core.ui.settings.confirm_discard_desc": "Descartar todas as alterações pendentes nas configurações?",
    "core.ui.settings.confirm_close_title": "Descartar e fechar?",
    "core.ui.settings.confirm_close_desc": "Você tem alterações não salvas. Descartar e fechar as configurações?",

    "core.ui.wall_height.low": "Baixa",
    "core.ui.wall_height.low_desc": "Baixa para facilitar a visualização.",
    "core.ui.wall_height.medium": "Média",
    "core.ui.wall_height.medium_desc": "Média para planejar.",
    "core.ui.wall_height.high": "Alta",
    "core.ui.wall_height.high_desc": "Alta (altura normal de parede).",

    "core.ui.layers_group_walls": "Paredes",
    "core.ui.layers_group_areas": "Áreas",
    "core.ui.layers_group_background": "Fundo",
    "core.ui.layers.hide": "Ocultar camada",
    "core.ui.layers.show": "Mostrar camada",
    "core.ui.layers.lock": "Bloquear movimentação",
    "core.ui.layers.unlock": "Desbloquear movimentação",
    "core.ui.layers.reorder": "Reordenar camada",

    "core.tools.navigate": "Navegar",
    "core.tools.navigate_desc": "Mover o canvas.",
    "core.tools.select": "Selecionar",
    "core.tools.select_desc": "Selecionar e mover elementos.",

    "core.ui.empty_title": "Nada configurado ainda",
    "core.ui.empty_desc": "Clique em “Editar” para adicionar elementos na composição.",
    "core.ui.notifications_empty": "Nenhuma notificação por enquanto.",
    "core.ui.image_preview": "Imagem",
    "core.ui.loading": "Carregando…",
    "core.ui.error": "Erro",
    "core.ui.element_types_empty": "Nenhuma extensão registrou elementos ainda.",
    "core.ui.layers_empty": "Nenhum elemento adicionado ainda.",

    "core.ui.render_modal.title": "Renderização",
    "core.ui.render_modal.option_3d.title": "3D (ThreeJS)",
    "core.ui.render_modal.option_3d.desc": "Visualização 3D interativa.",
    "core.ui.render_modal.option_2d.title": "2D (Captura)",
    "core.ui.render_modal.option_2d.desc": "Captura de cima com overlays do Home Assistant.",

    "core.ui.main2d.cluster.title": "Vários itens ({{count}})",
    "core.ui.main2d.cluster.tooltip": "{{count}} itens",

    "core.ui.action_unavailable": "Sem ações disponíveis para este elemento.",

    "core.compositions.modal.title": "Composições",
    "core.compositions.section.new": "Nova composição",
    "core.compositions.section.list": "Suas composições",
    "core.compositions.new.placeholder": "Nome (ex: Térreo, Superior...)",
    "core.compositions.delete_confirm": "Excluir “{{name}}”?",
    "core.compositions.cannot_delete_last": "Não é possível excluir a última composição",
    "core.compositions.aria.create": "Criar composição",
    "core.compositions.aria.save_name": "Salvar nome",
    "core.compositions.aria.cancel": "Cancelar",
    "core.compositions.aria.rename": "Renomear composição",
    "core.compositions.aria.delete": "Excluir composição",
    "core.compositions.aria.cancel_delete": "Cancelar exclusão",
    "core.compositions.aria.confirm_delete": "Confirmar exclusão",
    "core.compositions.error.create": "Falha ao criar composição",
    "core.compositions.error.activate": "Falha ao trocar composição",
    "core.compositions.error.rename": "Falha ao renomear composição",
    "core.compositions.error.delete": "Falha ao excluir composição",

    "core.element_editor.title": "Editar elemento",
    "core.element_editor.name": "Nome",
    "core.element_editor.pos_x": "Posição X",
    "core.element_editor.pos_y": "Posição Y",
    "core.element_editor.pos_z": "Posição Z",
    "core.element_editor.rot_x": "Rotação X (graus)",
    "core.element_editor.rot_y": "Rotação Y (graus)",
    "core.element_editor.rot_z": "Rotação Z (graus)",
    "core.element_editor.delete": "Excluir elemento",

    "core.modal.aria.close": "Fechar",
  },
};

let locale: Locale = resolveInitialLocale();
const listeners = new Set<() => void>();

function isLocale(value: unknown): value is Locale {
  return value === "en" || value === "pt-BR";
}

function safeGetStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSetStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // ignore
  }
}

function resolveInitialLocale(): Locale {
  const stored = safeGetStorage(STORAGE_KEY);
  if (isLocale(stored)) return stored;

  const nav = typeof navigator !== "undefined" ? navigator.language : "en";
  const lower = String(nav).toLowerCase();
  if (lower.startsWith("pt")) return "pt-BR";
  return "en";
}

function notify(): void {
  for (const l of listeners) l();
}

function interpolate(template: string, params: Record<string, unknown>): string {
  return template.replace(/{{\s*([\w.-]+)\s*}}/g, (_m, key: string) => {
    const value = params[key];
    if (value === null || value === undefined) return "";
    return String(value);
  });
}

export const i18n: I18nApi = {
  getLocale(): Locale {
    return locale;
  },
  setLocale(next: Locale): void {
    if (next === locale) return;
    locale = next;
    safeSetStorage(STORAGE_KEY, next);
    notify();
  },
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  registerTranslations(bundle: TranslationBundle): void {
    let changed = false;
    for (const [locKey, resources] of Object.entries(bundle)) {
      if (!isLocale(locKey)) continue;
      if (!resources) continue;
      Object.assign(translationsByLocale[locKey], resources);
      changed = true;
    }
    if (changed) notify();
  },
  t(key: string, params: Record<string, unknown> = {}, fallback?: string): string {
    const dict = translationsByLocale[locale];
    const base = dict[key] ?? translationsByLocale.en[key] ?? fallback ?? key;
    return Object.keys(params).length ? interpolate(base, params) : base;
  },
  useI18n(): { locale: Locale; t: I18nApi["t"]; setLocale: I18nApi["setLocale"] } {
    const current = useSyncExternalStore<Locale>(i18n.subscribe, i18n.getLocale, i18n.getLocale);
    return { locale: current, t: i18n.t, setLocale: i18n.setLocale };
  },
};

export function resolveLocalizedString(value: LocalizedString | undefined): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  return i18n.t(value.key, value.params ?? {}, value.fallback);
}
