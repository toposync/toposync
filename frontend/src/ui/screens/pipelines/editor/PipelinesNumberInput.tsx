import React from "react";

type Props = Omit<React.InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange"> & {
  value: number;
  onChange: (value: number) => void;
};

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return "";
  return String(value);
}

function isIntermediateNumberText(text: string): boolean {
  // Allow the browser default behavior while the user is typing:
  // "", "-", ".", "-." are valid intermediate states.
  return text === "" || text === "-" || text === "." || text === "-.";
}

export function PipelinesNumberInput({ value, onChange, onBlur, onFocus, onKeyDown, className, ...props }: Props): React.ReactElement {
  const formatted = formatNumber(value);
  const [isFocused, setIsFocused] = React.useState(false);
  const [text, setText] = React.useState(formatted);

  React.useEffect(() => {
    if (isFocused) return;
    setText(formatted);
  }, [formatted, isFocused]);

  return (
    <input
      {...props}
      className={className}
      type="number"
      value={text}
      onFocus={(event) => {
        setIsFocused(true);
        onFocus?.(event);
      }}
      onBlur={(event) => {
        setIsFocused(false);
        setText(formatted);
        onBlur?.(event);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          (event.currentTarget as HTMLInputElement).blur();
          return;
        }
        if (event.key === "Escape") {
          setText(formatted);
          (event.currentTarget as HTMLInputElement).blur();
          return;
        }
        onKeyDown?.(event);
      }}
      onChange={(event) => {
        const raw = event.target.value;
        setText(raw);
        if (isIntermediateNumberText(raw)) return;
        const next = Number(raw);
        if (!Number.isFinite(next)) return;
        onChange(next);
      }}
    />
  );
}
