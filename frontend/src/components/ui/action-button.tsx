import type { ButtonHTMLAttributes, ReactNode } from "react";

type ActionButtonVariant = "neutral" | "warning" | "success";

const variantClasses: Record<ActionButtonVariant, string> = {
  neutral: "border-zinc-300 text-zinc-600 hover:bg-zinc-50",
  warning: "border-amber-300 text-amber-700 hover:bg-amber-50",
  success: "border-green-300 text-green-700 hover:bg-green-50",
};

export function ActionButton({
  variant = "neutral",
  children,
  className = "",
  ...props
}: {
  variant?: ActionButtonVariant;
  children: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={`inline-flex items-center gap-1 text-xs px-2 py-1 border rounded disabled:opacity-50 ${variantClasses[variant]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
