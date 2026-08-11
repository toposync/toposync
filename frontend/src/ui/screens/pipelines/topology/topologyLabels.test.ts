declare const require: any;

import { i18n } from "../../../../util/i18n";

import {
  pressureBehaviorLabel,
  pressureCauseLabel,
  pressureStateLabel,
  resourceKindLabel,
  runtimeStateLabel,
} from "./topologyLabels";

const test: (name: string, fn: () => void | Promise<void>) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

test("localizes dynamic topology telemetry values in Portuguese", () => {
  const originalLocale = i18n.getLocale();
  try {
    i18n.setLocale("pt-BR");

    assert.equal(runtimeStateLabel("active", i18n.t), "ativo");
    assert.equal(resourceKindLabel("vision_model", i18n.t), "modelo de visão");
    assert.equal(pressureStateLabel("critical", i18n.t), "crítica");
    assert.equal(pressureBehaviorLabel("skip_before_compute", i18n.t), "pular antes de processar");
    assert.equal(pressureCauseLabel("structural_backlog", i18n.t), "acúmulo estrutural");
  } finally {
    i18n.setLocale(originalLocale);
  }
});

test("keeps unknown telemetry values observable", () => {
  assert.equal(runtimeStateLabel("vendor_state", i18n.t), "vendor_state");
});
