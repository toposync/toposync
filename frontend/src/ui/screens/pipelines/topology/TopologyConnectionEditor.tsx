import { useId, useState } from "react";
import type { Connection } from "@xyflow/react";
import { i18n } from "../../../../util/i18n";
import type { TopologyModel, TopologyNode } from "./topologyTypes";

type Props = {
  node: TopologyNode;
  model: TopologyModel;
  editable: boolean;
  onConnect?: (connection: Connection) => string | null;
};

export function TopologyConnectionEditor({ node, model, editable, onConnect }: Props) {
  const { t } = i18n.useI18n();
  const id = useId();
  const [output, setOutput] = useState(node.data.outputPorts[0] ?? "");
  const [destination, setDestination] = useState("");
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null);
  const targets = model.nodes.flatMap((target) => target.id === node.id ? [] : target.data.inputPorts.map((port) => ({
    value: JSON.stringify([target.id, port]),
    nodeId: target.id,
    port,
    label: `${target.data.label} (${target.id}) — ${port}`,
  })));
  const sourcePort = node.data.outputPorts.includes(output) ? output : "";
  const target = targets.find((option) => option.value === destination);

  if (!editable || !onConnect || node.data.outputPorts.length === 0) return null;

  const connect = () => {
    if (!editable || !sourcePort || !target || !onConnect) return;
    const error = onConnect({ source: node.id, sourceHandle: sourcePort, target: target.nodeId, targetHandle: target.port });
    setFeedback({ error: error !== null, text: error ?? t("core.ui.pipelines.topology.connection.success", {}, "Connection added. Save the pipeline to keep it.") });
  };

  return (
    <section className="pipelineTopologyInspectorSection" aria-labelledby={`${id}-title`}>
      <div id={`${id}-title`} className="pipelineTopologyInspectorSectionTitle">
        {t("core.ui.pipelines.topology.connection.title", {}, "Connect nodes")}
      </div>
      <div className="pipelineTopologyEditGrid">
        <label htmlFor={`${id}-output`}>
          <span>{t("core.ui.pipelines.topology.connection.output", {}, "Output port")}</span>
          <select id={`${id}-output`} value={sourcePort} onChange={(event) => { setOutput(event.target.value); setFeedback(null); }}>
            {!sourcePort ? <option value="">{t("core.ui.pipelines.topology.connection.choose", {}, "Choose a port")}</option> : null}
            {node.data.outputPorts.map((port) => <option key={port} value={port}>{port}</option>)}
          </select>
        </label>
        <label htmlFor={`${id}-destination`}>
          <span>{t("core.ui.pipelines.topology.connection.destination", {}, "Destination node and input port")}</span>
          <select id={`${id}-destination`} value={target?.value ?? ""} aria-describedby={`${id}-hint`}
            onChange={(event) => { setDestination(event.target.value); setFeedback(null); }}>
            <option value="">{t("core.ui.pipelines.topology.connection.choose", {}, "Choose a port")}</option>
            {targets.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </label>
        <div id={`${id}-hint`} className="pipelineTopologyInspectorNotice">
          {t("core.ui.pipelines.topology.connection.hint", {}, "Choose the receiving node and port. Occupied inputs and cycles are rejected. New connections use the operators' default queue policies.")}
        </div>
        <button className="pillButton" type="button" disabled={!sourcePort || !target} onClick={connect}>
          {t("core.ui.pipelines.topology.connection.connect", {}, "Connect")}
        </button>
        <div role="status" aria-live="polite">{feedback && !feedback.error ? feedback.text : null}</div>
        {feedback?.error ? <div className="pipelineTopologyInspectorNotice" role="alert">{feedback.text}</div> : null}
      </div>
    </section>
  );
}
