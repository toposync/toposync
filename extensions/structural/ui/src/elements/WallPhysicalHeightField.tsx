import React, { useEffect, useId, useState } from "react";

import { parsePhysicalHeightInput, readPhysicalHeightMeters } from "../wallPhysicalHeight";

type Props = {
  value: unknown;
  onCommit: (value: number | null) => void;
  label: string;
  hint: string;
  error: string;
  unknown: string;
};

export function WallPhysicalHeightField(props: Props): React.ReactElement {
  const value = readPhysicalHeightMeters(props.value);
  const [draft, setDraft] = useState(value === null ? "" : String(value));
  const [dirty, setDirty] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const id = useId();
  useEffect(() => {
    setDraft(value === null ? "" : String(value));
    setDirty(false);
    setInvalid(false);
  }, [value]);

  function commit(): void {
    if (!dirty) return;
    const parsed = parsePhysicalHeightInput(draft);
    setInvalid(!parsed.valid);
    if (!parsed.valid) return;
    props.onCommit(parsed.value);
    setDirty(false);
  }

  return (
    <div className="field">
      <label className="label" htmlFor={id}>{props.label}</label>
      <input
        id={id}
        className="input"
        type="text"
        inputMode="decimal"
        value={draft}
        placeholder={props.unknown}
        aria-invalid={invalid || undefined}
        aria-describedby={`${id}-hint${invalid ? ` ${id}-error` : ""}`}
        onChange={(event) => {
          setDraft(event.target.value);
          setDirty(true);
          setInvalid(false);
        }}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            event.currentTarget.blur();
          } else if (event.key === "Escape") {
            setDraft(value === null ? "" : String(value));
            setDirty(false);
            setInvalid(false);
          }
        }}
      />
      <div id={`${id}-hint`} className="cardBody">{props.hint}</div>
      {invalid ? <div id={`${id}-error`} className="cardBody" role="alert">{props.error}</div> : null}
    </div>
  );
}
