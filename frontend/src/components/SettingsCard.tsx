/**
 * Shared Settings card primitives.
 *
 * Used by the upstream detail page (view + edit modes) and the
 * upstream create wizard so the operator's eye lands on the same
 * row in the same place across all three surfaces.
 *
 * - ``SettingsCard``: bordered white card with a header bar.
 * - ``SettingsField``: uppercase label + body row used inside a
 *   card to keep vertical rhythm consistent. The body is whatever
 *   the caller renders — a control, a readout span, a custom
 *   widget — so the same card can serve view, edit, and create.
 */
import type { JSX, ReactNode } from "react";

export function SettingsCard({
  title, children, actions,
}: {
  title: string;
  children: ReactNode;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <div className="rounded-lg border border-zinc-200 bg-white mb-4 shadow-sm">
      <div className="px-4 py-2.5 border-b border-zinc-100 flex items-center gap-2">
        <span className="text-sm font-semibold text-zinc-700">{title}</span>
        {actions && <div className="ml-auto">{actions}</div>}
      </div>
      <div className="px-4 py-3 space-y-4">{children}</div>
    </div>
  );
}

export function SettingsField({
  label, hint, children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}): JSX.Element {
  return (
    <div>
      <label className="block text-xs font-medium uppercase tracking-wide text-zinc-500 mb-1.5">
        {label}
      </label>
      {children}
      {hint && <div className="mt-1">{hint}</div>}
    </div>
  );
}
