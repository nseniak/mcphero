interface FieldHintProps {
  children: React.ReactNode;
  inline?: boolean;
  /** When ``true`` the hint renders in red (text-red-600) — used
   *  for inline validation errors like "Incomplete or invalid JSON".
   *  Default tone is informational (text-zinc-500). */
  error?: boolean;
}

export function FieldHint({ children, inline, error }: FieldHintProps) {
  const tone = error ? "text-red-600" : "text-zinc-500";
  const spacing = inline ? "" : "mt-1";
  return <p className={`text-xs ${tone} ${spacing}`}>{children}</p>;
}
