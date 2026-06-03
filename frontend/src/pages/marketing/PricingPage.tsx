import { useEffect } from "react";
import { Link } from "react-router";
import { PricingTier } from "../../components/marketing/PricingTier";
import { Seo } from "../../components/marketing/Seo";
import {
  organizationSchema,
  productSchema,
} from "../../components/marketing/structuredData";
import { useAuth } from "../../hooks/useAuth";
import { track } from "../../lib/analytics";
import { handlePlanLimitError } from "../../lib/planLimits";
import { PlanLimitError } from "../../api/client";
import { getLoginUrl } from "../../api/auth";

export function PricingPage() {
  const { user } = useAuth();
  const currentPlan = user?.current_org?.plan ?? null;
  const signedIn = !!user;

  useEffect(() => {
    track("pricing_page_viewed", {
      signed_in: signedIn,
      plan: currentPlan,
    });
  }, [signedIn, currentPlan]);

  function openUpgradeDialog() {
    handlePlanLimitError(
      new PlanLimitError("Upgrade to Team", "pricing_page_card", null, null),
      { source: "pricing_page_card" },
    );
  }

  const isFreeCurrent = currentPlan === "free";
  const isTeamCurrent = currentPlan === "team";

  return (
    <>
      <Seo
        title="Pricing"
        description="Simple pricing for your whole team. Start free. Pay when you're ready to roll out."
        path="/pricing"
        jsonLd={[organizationSchema, productSchema]}
      />
      <section className="px-6 pt-20 pb-10 md:pt-24">
        <div className="max-w-4xl mx-auto text-center space-y-4">
          <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">Pricing</p>
          <h1 className="text-4xl md:text-5xl font-semibold text-zinc-900 tracking-tight">
            Simple pricing for your whole team.
          </h1>
          <p className="text-lg text-zinc-600 max-w-2xl mx-auto">
            Start free. Pay when you&apos;re ready to roll out.
          </p>
        </div>
      </section>

      <section className="px-6 pb-16">
        <div className="max-w-4xl mx-auto grid md:grid-cols-2 gap-4">
          <div className="relative">
            {isFreeCurrent && (
              <span
                className="absolute -top-2 left-4 px-2 py-0.5 bg-green-100 text-green-700 rounded text-xs font-medium z-10"
                data-testid="current-plan-badge"
              >
                Your current plan
              </span>
            )}
            <PricingTier
              name="Free"
              price="$0"
              description="Get your team trying remote HTTP MCPs in minutes. No credit card."
              features={[
                "Up to 3 teammates",
                "Up to 5 remote HTTP MCPs",
                "1 hosted stdio MCP",
                "Role-based permissions (admin + user)",
                "30-day audit log",
              ]}
              ctaLabel={isFreeCurrent ? "Your current plan" : "Get started free"}
              ctaTo={isFreeCurrent || !signedIn ? undefined : "/signup"}
              ctaHref={!signedIn ? getLoginUrl() : undefined}
              ctaId="pricing_free_cta"
              onCtaClick={isFreeCurrent ? () => undefined : undefined}
            />
          </div>
          <div className="relative">
            {isTeamCurrent && (
              <span
                className="absolute -top-2 left-4 px-2 py-0.5 bg-green-100 text-green-700 rounded text-xs font-medium z-10"
                data-testid="current-plan-badge"
              >
                Your current plan
              </span>
            )}
            <PricingTier
              name="Team"
              price="$19"
              cadence="mo"
              description="Up to 10 teammates included. +$5/seat/mo above 10."
              features={[
                "Unlimited MCPs",
                "Custom roles",
                "MCP tool argument checks",
                "1-year audit retention",
                "Priority email support",
                "Everything in Free",
              ]}
              ctaLabel={
                !signedIn
                  ? undefined
                  : isTeamCurrent
                  ? "Your current plan"
                  : "Upgrade"
              }
              ctaId={signedIn ? "pricing_team_cta" : undefined}
              onCtaClick={
                !signedIn
                  ? undefined
                  : isTeamCurrent
                  ? () => undefined
                  : openUpgradeDialog
              }
              highlighted
            />
          </div>
        </div>
        <div className="max-w-4xl mx-auto mt-6 text-center space-y-2">
          <p className="text-sm text-zinc-600">
            Need SSO, compliance, or dedicated infrastructure?{" "}
            <a
              href="mailto:hello@mcphero.io?subject=Enterprise%20pricing"
              className="text-zinc-900 underline hover:no-underline"
            >
              Contact us
            </a>
          </p>
          <p className="text-xs text-zinc-500">
            Hosted stdio MCPs are subject to{" "}
            <Link to="/terms#fair-use" className="underline hover:no-underline">
              fair use
            </Link>
            .
          </p>
        </div>
      </section>
    </>
  );
}
