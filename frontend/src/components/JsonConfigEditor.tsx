/**
 * JSON config editor. The annotation panel that used to render
 * underneath the textarea moved into the Environment-variables card
 * (only the unresolved-references list survives there); this stays a
 * thin textarea wrapper so the call sites can keep importing it.
 */
import type { JSX } from "react";

interface Props {
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  className?: string;
  placeholder?: string;
  disabled?: boolean;
}

export function JsonConfigEditor({
  value,
  onChange,
  rows = 32,
  className,
  placeholder,
  disabled,
}: Props): JSX.Element {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      disabled={disabled}
      placeholder={placeholder}
      className={
        className ??
        "w-full font-mono text-xs px-3 py-2 border border-zinc-300 rounded bg-zinc-50 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:bg-white"
      }
    />
  );
}
