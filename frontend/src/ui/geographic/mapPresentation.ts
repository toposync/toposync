import type { CompositionElement, ElementType } from "@toposync/plugin-api";

export function geographicElementLabel(element: CompositionElement, definition: ElementType | undefined, translate: (key: string) => string): string {
  const type = typeof definition?.name === "string" ? definition.name : definition?.name ? translate(definition.name.key) : translate("core.map.element");
  return element.name?.trim() || type;
}
