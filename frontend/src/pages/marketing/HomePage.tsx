import { Link, Navigate, useLocation } from "react-router";
import { Cloud, Network, SlidersHorizontal } from "lucide-react";
import { Hero } from "../../components/marketing/Hero";
import { SectionHeading } from "../../components/marketing/SectionHeading";
import { FeatureCard } from "../../components/marketing/FeatureCard";
import { GatewayDiagram } from "../../components/marketing/GatewayDiagram";
import { Seo } from "../../components/marketing/Seo";
import {
  organizationSchema,
  softwareApplicationSchema,
} from "../../components/marketing/structuredData";
import { getLoginUrl } from "../../api/auth";
import { useAuth } from "../../hooks/useAuth";
import { useFeatures } from "../../hooks/useFeatures";
import { track } from "../../lib/analytics";

interface HomePageProps {
  forceShow?: boolean;
}

export function HomePage({ forceShow = false }: HomePageProps) {
  const { mode, isLoading: featuresLoading } = useFeatures();
  const { user, loading: authLoading } = useAuth();
  const { pathname } = useLocation();
  // `__PRERENDER_INJECTED` is set by the build-time prerender plugin so
  // crawlers (and previewers like LinkedIn) get marketing content rather
  // than a redirect to the auth-gated /app shell. We also short-circuit
  // the loading gate in prerender, since /api/features and /api/auth 404
  // against the prerenderer's static server and would otherwise leave the
  // hooks stuck in `loading=true` forever.
  const isPrerender =
    typeof window !== "undefined" &&
    (window as unknown as { __PRERENDER_INJECTED?: boolean }).__PRERENDER_INJECTED === true;
  // Loading-gate only when we might redirect — i.e. forceShow is
  // false and we still need ``mode`` / ``user`` to decide. With
  // forceShow=true the body never branches on auth or features, so
  // returning null during the /api/config/features round-trip just
  // creates a blank-flash when navigating in from another page (the
  // hook re-fires on every mount). Render the marketing content
  // immediately in that case.
  const needsRedirectDecision = !forceShow && !isPrerender;
  if (needsRedirectDecision && (featuresLoading || authLoading)) return null;
  if (needsRedirectDecision) {
    if (mode === "standalone") return <Navigate to="/app" replace />;
    if (user) return <Navigate to="/app" replace />;
  }
  return (
    <>
      <Seo
        title="MCP Hero"
        description="You got MCPs working for yourself. Now do it for your team — every teammate uses your MCPs from Claude, ChatGPT, or Cursor, scoped to their role."
        path="/"
        jsonLd={[organizationSchema, softwareApplicationSchema]}
      />
      <Hero
        title="You got MCPs working for yourself. Now do it for your team."
        subtitle="Add an MCP once. We manage it for your team. Any team member uses it from any AI client, scoped to the role you set."
        primaryCta={{ label: "Get started on our free plan", href: getLoginUrl(), ctaId: "hero_get_started" }}
        secondaryCta={{ label: "Read the docs", to: "/docs", ctaId: "hero_see_docs" }}
        art={<GatewayDiagram />}
      />

      <section className="px-6 py-16 md:py-20 bg-zinc-50 border-y border-zinc-200">
        <div className="max-w-6xl mx-auto space-y-12">
          <SectionHeading
            eyebrow="Why MCP Hero"
            title="Built for the person rolling out AI."
          />
          <div className="grid md:grid-cols-3 gap-4">
            <FeatureCard
              icon={<Network className="h-5 w-5" />}
              title="Manage your team's MCPs in one place"
              description="You configure your MCPs once in MCP Hero. Your team gets a single URL to paste into Claude Desktop, Claude Code, ChatGPT, Cursor, or any other MCP client. All the tools show up in all of those clients without repetitive per-client setup. When you add a new MCP, everyone has it on their next request."
            />
            <FeatureCard
              icon={<Cloud className="h-5 w-5" />}
              title="Leave the plumbing to us"
              description="MCP Hero is a hosted service — there's nothing to install or run on your side. We keep MCP connections alive so your team members don't get disconnected all the time. We run stdio MCP code inside secure sandboxes so your team's machines stay clean."
            />
            <FeatureCard
              icon={<SlidersHorizontal className="h-5 w-5" />}
              title="Control who accesses what"
              description="You assign each team member a role. Roles decide which MCPs they can use and which tools within each MCP. You can even pin or block specific arguments — for example, force GitHub calls to your org or block any SQL containing DROP. Adding or removing a user is one click."
            />
          </div>
        </div>
      </section>

      <section className="px-6 py-20 md:py-24">
        <div className="max-w-3xl mx-auto text-center space-y-6">
          <h2 className="text-3xl md:text-4xl font-semibold text-zinc-900">
            Ready to share MCPs with your team?
          </h2>
          <p className="text-zinc-600 text-base md:text-lg">
            Create your organization in under a minute. Invite your team, add your first MCP, and
            set who can use what.
          </p>
          <a
            href={getLoginUrl()}
            onClick={() =>
              track("landing_cta_clicked", {
                cta_id: "home_bottom_create_org",
                page: pathname,
              })
            }
            className="inline-flex items-center px-5 py-2.5 rounded-md bg-zinc-900 text-white text-sm font-medium hover:bg-zinc-800 transition-colors"
          >
            Create your organization
          </a>
          <p className="text-sm text-zinc-500">
            Have questions first?{" "}
            <Link
              to="/contact"
              onClick={() =>
                track("landing_cta_clicked", {
                  cta_id: "home_bottom_contact",
                  page: pathname,
                })
              }
              className="text-zinc-900 underline underline-offset-2 hover:text-zinc-700 transition-colors"
            >
              Talk to us
            </Link>
            .
          </p>
        </div>
      </section>
    </>
  );
}
