import React from "react";

type Props = {
  name: string;
  className?: string;
};

export function Icon({ name, className }: Props): React.ReactElement {
  const cls = ["fa-solid", `fa-${name}`, className].filter(Boolean).join(" ");
  return <i className={cls} aria-hidden="true" />;
}

