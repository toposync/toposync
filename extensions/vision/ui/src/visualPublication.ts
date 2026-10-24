import type { VisualClass, VisualConfiguration } from "./visualState";

export type VisualPublication = { enabled: boolean; kind: "enum" | "binary"; name: string; on_class_ids: string[]; off_class_ids: string[] };
export const publicationDefaults = (): VisualPublication => ({ enabled: false, kind: "enum", name: "", on_class_ids: [], off_class_ids: [] });
export function visualPublication(value: VisualConfiguration): VisualPublication {
  return { ...publicationDefaults(), ...(value.home_assistant as Partial<VisualPublication> | undefined) };
}
export function publicationIssue(value: VisualConfiguration, fields: VisualPublication): string | null {
  if (typeof fields.enabled !== "boolean" || !["enum", "binary"].includes(fields.kind)
    || typeof fields.name !== "string" || fields.name.trim().length > 80
    || ![fields.on_class_ids, fields.off_class_ids].every(ids => Array.isArray(ids) && ids.length <= 32
      && ids.every(id => typeof id === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(id)))) return "publication_invalid";
  const identifiers = [...fields.on_class_ids, ...fields.off_class_ids];
  if (new Set(identifiers).size !== identifiers.length) return "publication_invalid";
  if (identifiers.some(id => !value.classes.some(category => category?.class_id === id))) return "publication_missing_class";
  if (fields.enabled && fields.kind === "binary" && [fields.on_class_ids, fields.off_class_ids].some(ids =>
    !value.classes.some(category => category?.enabled && ids.includes(category.class_id)))) return "publication_need_sides";
  return null;
}
export function publicationClassSide(fields: VisualPublication, classId: string): "on" | "off" | "unknown" {
  if (Array.isArray(fields.on_class_ids) && fields.on_class_ids.includes(classId)) return "on";
  if (Array.isArray(fields.off_class_ids) && fields.off_class_ids.includes(classId)) return "off";
  return "unknown";
}
export function publicationClassValue(fields: VisualPublication, category: VisualClass): string {
  if (!category.enabled) return "disabled";
  return fields.kind === "enum" ? category.key : publicationClassSide(fields, category.class_id);
}
export function mapPublicationClass(fields: VisualPublication, classId: string, side: "on" | "off" | "unknown"): VisualPublication {
  return { ...fields,
    on_class_ids: [...(Array.isArray(fields.on_class_ids) ? fields.on_class_ids : []).filter(id => id !== classId), ...(side === "on" ? [classId] : [])],
    off_class_ids: [...(Array.isArray(fields.off_class_ids) ? fields.off_class_ids : []).filter(id => id !== classId), ...(side === "off" ? [classId] : [])],
  };
}
export function applyVisualPublication(value: VisualConfiguration, fields: VisualPublication): VisualConfiguration {
  const issue = publicationIssue(value, fields);
  if (issue) throw new Error(issue);
  return { ...value, home_assistant: { ...fields, name: fields.name.trim() } };
}
