import React from "react";
import type { HostI18n, Notification } from "@toposync/plugin-api";
import { readRecord, readString } from "../parsing";

export function supportsStationaryObservation(notification: Notification): boolean {
  return notification.type === "pipelines.tracking" && Object.keys(readRecord(readRecord(readRecord(notification.payload).data).stationary_event)).length > 0;
}

export function StationaryObservationSummary({ notification, i18n }: { notification: Notification; i18n: HostI18n }) {
  const { t, locale } = i18n.useI18n();
  const payload = readRecord(notification.payload);
  const data = readRecord(payload.data);
  const subject = readRecord(payload.subject);
  const category = readString(subject.category) || readString(readRecord(data.subject).category);
  const known = ["person", "car", "truck", "motorcycle", "bicycle", "bus", "dog", "cat"];
  const name = known.includes(category) ? t(`ext.cameras.observation.${category}`) : category;
  const velocity = readRecord(data.velocity);
  const speed = velocity.speed_kmh;
  const stopped = readRecord(data.stationary_event).stationary_seconds;
  const duration = readRecord(payload.event).duration_seconds;
  const number = (value: unknown): string | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value.toLocaleString(locale, { maximumFractionDigits: 1 }) : null;
  const durationLabel = (value: unknown): string | null => {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
    if (value < 60) return `${number(value)} s`;
    return `${Math.floor(value / 60)} min ${Math.floor(value % 60)} s`;
  };
  const seconds = durationLabel(stopped);
  return <div className="notificationCameraBody">
    <div className="notificationCameraName">{[name, readString(data.area_label)].filter(Boolean).join(" · ")}</div>
    <div className="notificationText">{[
      velocity.valid === true && number(speed) !== null ? `${number(speed)} km/h` : "",
      seconds ? t("ext.cameras.observation.stopped", { time: seconds }) : "",
    ].filter(Boolean).join(" · ")}</div>
    {durationLabel(duration) && <div className="cardMeta">{t("ext.cameras.observation.duration", { time: durationLabel(duration) })}</div>}
  </div>;
}

export const stationaryObservationTranslations = {
  en: {
    "ext.cameras.observation.dog": "Dog", "ext.cameras.observation.cat": "Cat",
    "ext.cameras.observation.person": "Person", "ext.cameras.observation.car": "Car", "ext.cameras.observation.truck": "Truck",
    "ext.cameras.observation.motorcycle": "Motorcycle", "ext.cameras.observation.bicycle": "Bicycle", "ext.cameras.observation.bus": "Bus",
    "ext.cameras.observation.stopped": "Stopped: {{time}}", "ext.cameras.observation.duration": "Occurrence: {{time}}",
  },
  "pt-BR": {
    "ext.cameras.observation.dog": "Cachorro", "ext.cameras.observation.cat": "Gato",
    "ext.cameras.observation.person": "Pessoa", "ext.cameras.observation.car": "Carro", "ext.cameras.observation.truck": "Caminhão",
    "ext.cameras.observation.motorcycle": "Motocicleta", "ext.cameras.observation.bicycle": "Bicicleta", "ext.cameras.observation.bus": "Ônibus",
    "ext.cameras.observation.stopped": "Tempo parado: {{time}}", "ext.cameras.observation.duration": "Acontecimento: {{time}}",
  },
};
