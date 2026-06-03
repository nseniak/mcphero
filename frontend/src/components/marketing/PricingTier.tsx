import { Link, useLocation } from "react-router";
import { track } from "../../lib/analytics";

interface PricingTierProps {
  name: string;
  price: string;
  cadence?: string;
  description: string;
  features: string[];
  ctaLabel?: string;
  ctaTo?: string;
  ctaHref?: string;
  ctaId?: string;
  onCtaClick?: () => void;
  highlighted?: boolean;
}

export function PricingTier({
  name,
  price,
  cadence,
  description,
  features,
  ctaLabel,
  ctaTo,
  ctaHref,
  ctaId,
  onCtaClick,
  highlighted = false,
}: PricingTierProps) {
  const { pathname } = useLocation();
  const border = highlighted ? "border-zinc-900 shadow-md" : "border-zinc-200";
  const cta = highlighted
    ? "bg-zinc-900 text-white hover:bg-zinc-800"
    : "bg-white text-zinc-900 border border-zinc-300 hover:bg-zinc-50";
  const ctaClassName = `w-full inline-flex items-center justify-center px-4 py-2 rounded-md text-sm font-medium transition-colors ${cta}`;
  const fireTrack = () => track("landing_cta_clicked", { cta_id: ctaId, page: pathname });
  return (
    <div className={`rounded-xl border ${border} bg-white p-6 flex flex-col space-y-5 h-full`}>
      <div className="space-y-2">
        <h3 className="text-lg font-semibold text-zinc-900">{name}</h3>
        <div className="flex items-baseline gap-1">
          <span className="text-3xl font-semibold text-zinc-900">{price}</span>
          {cadence && <span className="text-sm text-zinc-500">/{cadence}</span>}
        </div>
        <p className="text-sm text-zinc-600">{description}</p>
      </div>
      <ul className="space-y-2 text-sm text-zinc-700 flex-1">
        {features.map((f) => (
          <li key={f} className="flex gap-2">
            <span className="text-zinc-400">•</span>
            <span>{f}</span>
          </li>
        ))}
      </ul>
      {onCtaClick ? (
        <button
          type="button"
          onClick={() => {
            fireTrack();
            onCtaClick();
          }}
          className={ctaClassName}
        >
          {ctaLabel}
        </button>
      ) : ctaHref ? (
        <a href={ctaHref} onClick={fireTrack} className={ctaClassName}>
          {ctaLabel}
        </a>
      ) : ctaTo ? (
        <Link to={ctaTo} onClick={fireTrack} className={ctaClassName}>
          {ctaLabel}
        </Link>
      ) : null}
    </div>
  );
}
