import type { ReactNode } from "react";

interface FeatureCardProps {
  icon?: ReactNode;
  title: string;
  description: string;
}

export function FeatureCard({ icon, title, description }: FeatureCardProps) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-6 space-y-3">
      {icon && <div className="text-zinc-900">{icon}</div>}
      <h3 className="text-base font-semibold text-zinc-900">{title}</h3>
      <p className="text-sm text-zinc-600 leading-relaxed">{description}</p>
    </div>
  );
}
