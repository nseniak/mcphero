import type { ReactNode } from "react";
import { Link, useLocation } from "react-router";
import { track } from "../../lib/analytics";

interface CtaSpec {
  label: string;
  to?: string;
  href?: string;
  ctaId: string;
}

interface HeroProps {
  eyebrow?: string;
  title: string;
  subtitle: string;
  primaryCta: CtaSpec;
  secondaryCta?: CtaSpec;
  art?: ReactNode;
}

function CtaButton({ cta, className }: { cta: CtaSpec; className: string }) {
  const { pathname } = useLocation();
  const onClick = () =>
    track("landing_cta_clicked", { cta_id: cta.ctaId, page: pathname });
  if (cta.href) {
    return (
      <a href={cta.href} onClick={onClick} className={className}>
        {cta.label}
      </a>
    );
  }
  return (
    <Link to={cta.to ?? "/"} onClick={onClick} className={className}>
      {cta.label}
    </Link>
  );
}

export function Hero({ eyebrow, title, subtitle, primaryCta, secondaryCta, art }: HeroProps) {
  const hasArt = Boolean(art);
  const copyAlign = hasArt ? "md:text-left text-center" : "text-center";
  const subtitleWidth = hasArt ? "md:mx-0 mx-auto" : "mx-auto";
  return (
    <section className="px-6 pt-20 pb-16 md:pt-28 md:pb-20">
      <div
        className={
          hasArt
            ? "max-w-6xl mx-auto grid md:grid-cols-[minmax(0,1fr)_minmax(0,420px)] md:gap-12 gap-10 items-start"
            : "max-w-4xl mx-auto"
        }
      >
        <div className={`${copyAlign} space-y-6`}>
          {eyebrow && (
            <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">{eyebrow}</p>
          )}
          <h1 className="text-4xl md:text-6xl font-semibold text-zinc-900 leading-tight tracking-tight">
            {title}
          </h1>
          <p className={`text-lg md:text-xl text-zinc-600 max-w-2xl ${subtitleWidth} leading-relaxed`}>
            {subtitle}
          </p>
          <div className="flex items-center justify-center gap-3 pt-2">
            <CtaButton
              cta={primaryCta}
              className="inline-flex items-center px-5 py-2.5 rounded-md bg-zinc-900 text-white text-sm font-medium hover:bg-zinc-800 transition-colors"
            />
            {secondaryCta && (
              <CtaButton
                cta={secondaryCta}
                className="inline-flex items-center px-5 py-2.5 rounded-md border border-zinc-300 bg-white text-zinc-900 text-sm font-medium hover:bg-zinc-50 transition-colors"
              />
            )}
          </div>
        </div>
        {hasArt && (
          <div className="flex items-center justify-center">
            {art}
          </div>
        )}
      </div>
    </section>
  );
}
