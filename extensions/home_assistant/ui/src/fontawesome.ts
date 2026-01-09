import houseSvg from "@fortawesome/fontawesome-free/svgs/solid/house.svg";
import lightbulbSvg from "@fortawesome/fontawesome-free/svgs/solid/lightbulb.svg";
import toggleOnSvg from "@fortawesome/fontawesome-free/svgs/solid/toggle-on.svg";
import fanSvg from "@fortawesome/fontawesome-free/svgs/solid/fan.svg";
import temperatureHalfSvg from "@fortawesome/fontawesome-free/svgs/solid/temperature-half.svg";
import lockSvg from "@fortawesome/fontawesome-free/svgs/solid/lock.svg";
import windowMaximizeSvg from "@fortawesome/fontawesome-free/svgs/solid/window-maximize.svg";
import videoSvg from "@fortawesome/fontawesome-free/svgs/solid/video.svg";
import tvSvg from "@fortawesome/fontawesome-free/svgs/solid/tv.svg";

import type { FontAwesomeIconFamilies, FontAwesomeIconSvg } from "./types";

export const BUILT_IN_FONT_AWESOME_SOLID_SVG_BY_NAME: Record<string, string> = {
  house: houseSvg,
  lightbulb: lightbulbSvg,
  "toggle-on": toggleOnSvg,
  fan: fanSvg,
  "temperature-half": temperatureHalfSvg,
  lock: lockSvg,
  "window-maximize": windowMaximizeSvg,
  video: videoSvg,
  tv: tvSvg,
};

let iconFamilies: FontAwesomeIconFamilies | null = null;
let iconFamiliesPromise: Promise<FontAwesomeIconFamilies> | null = null;

export function getFontAwesomeIconFamiliesCache(): FontAwesomeIconFamilies | null {
  return iconFamilies;
}

export function sanitizeFontAwesomeIconName(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/^fa-/, "")
    .replace(/[^a-z0-9-]/g, "")
    .slice(0, 64);
}

export function normalizeFontAwesomeSvgName(value: string): string {
  const key = sanitizeFontAwesomeIconName(value);
  if (key === "thermometer-half" || key === "thermometer") return "temperature-half";
  return key;
}

export function loadFontAwesomeIconFamilies(): Promise<FontAwesomeIconFamilies> {
  if (iconFamilies) return Promise.resolve(iconFamilies);
  if (iconFamiliesPromise) return iconFamiliesPromise;

  iconFamiliesPromise = import("@fortawesome/fontawesome-free/metadata/icon-families.json")
    .then((m: any) => (m.default ?? m) as FontAwesomeIconFamilies)
    .then((data) => {
      iconFamilies = data;
      return data;
    })
    .finally(() => {
      iconFamiliesPromise = null;
    });

  return iconFamiliesPromise;
}

function getSolidSvgFromFamilies(name: string): FontAwesomeIconSvg | null {
  const key = normalizeFontAwesomeSvgName(name);
  const entry = iconFamilies?.[key];
  const svg = entry?.svgs?.classic?.solid;
  if (!svg?.path || !svg?.viewBox?.length) return null;
  return svg;
}

function buildSvgFromSolid(svg: FontAwesomeIconSvg): string {
  const viewBox = svg.viewBox.join(" ");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}"><path d="${svg.path}"/></svg>`;
}

export function isFontAwesomeSolidIconAvailable(name: string): boolean {
  const key = normalizeFontAwesomeSvgName(name);
  if (BUILT_IN_FONT_AWESOME_SOLID_SVG_BY_NAME[key]) return true;
  return Boolean(getSolidSvgFromFamilies(key));
}

export function resolveFontAwesomeSvg(value: string): { key: string; svgText: string } {
  const key = normalizeFontAwesomeSvgName(value) || "house";

  const direct = BUILT_IN_FONT_AWESOME_SOLID_SVG_BY_NAME[key];
  if (direct) return { key, svgText: direct };

  const metaSvg = getSolidSvgFromFamilies(key);
  if (metaSvg) return { key, svgText: buildSvgFromSolid(metaSvg) };

  if (!iconFamilies && !iconFamiliesPromise) void loadFontAwesomeIconFamilies();

  return { key: "house", svgText: BUILT_IN_FONT_AWESOME_SOLID_SVG_BY_NAME.house };
}

