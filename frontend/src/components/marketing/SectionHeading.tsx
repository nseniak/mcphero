interface SectionHeadingProps {
  eyebrow?: string;
  title: string;
  subtitle?: string;
  align?: "left" | "center";
}

export function SectionHeading({ eyebrow, title, subtitle, align = "center" }: SectionHeadingProps) {
  const alignClass = align === "center" ? "text-center mx-auto" : "text-left";
  return (
    <div className={`max-w-2xl ${alignClass} space-y-3`}>
      {eyebrow && (
        <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">{eyebrow}</p>
      )}
      <h2 className="text-3xl md:text-4xl font-semibold text-zinc-900">{title}</h2>
      {subtitle && <p className="text-zinc-600 text-base md:text-lg">{subtitle}</p>}
    </div>
  );
}
