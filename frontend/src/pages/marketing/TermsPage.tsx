import { useEffect } from "react";
import { useLocation } from "react-router";
import { EmailLink } from "../../components/marketing/EmailLink";
import { Seo } from "../../components/marketing/Seo";
import { organizationSchema } from "../../components/marketing/structuredData";

const CONTACT_EMAIL =
  (import.meta.env.VITE_CONTACT_EMAIL as string | undefined) ?? "hello@mcphero.io";

export function TermsPage() {
  const { hash } = useLocation();
  useEffect(() => {
    if (!hash) return;
    const target = document.getElementById(hash.slice(1));
    target?.scrollIntoView({ behavior: "auto", block: "start" });
  }, [hash]);
  return (
    <>
      <Seo
        title="Terms of Service"
        description="The terms under which MCP Hero is provided."
        path="/terms"
        jsonLd={organizationSchema}
      />
      <article className="px-6 py-16 max-w-3xl mx-auto">
        <h1 className="text-3xl md:text-4xl font-semibold text-zinc-900 mb-2">Terms of Service</h1>
        <p className="text-sm text-zinc-500 mb-10">Last updated: 2026-05-08</p>

        <div className="space-y-6 text-zinc-700 leading-relaxed">
          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">1. Acceptance</h2>
            <p>
              By creating an account or using MCP Hero, you agree to these terms. If you are using
              MCP Hero on behalf of an organization, you represent that you have authority to bind
              that organization.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">2. Acceptable use</h2>
            <p>
              You agree not to abuse the service, attempt to bypass access controls, or use it to
              violate applicable laws.
            </p>
          </section>

          <section className="space-y-3" id="fair-use">
            <h2 className="text-xl font-semibold text-zinc-900">3. Fair use of hosted stdio MCPs</h2>
            <p>
              Hosted stdio MCPs run in isolated sandboxes that incur per-session compute costs.
              Plans that advertise unlimited hosted stdio MCPs are subject to fair use: typical
              interactive use — running MCP tools in the context of user chats with an AI
              assistant — is unmetered. Automated or headless workloads, and any mechanism that
              defeats sandbox auto-sleep (for example, keep-alives sent to prevent idle pause),
              fall outside fair use. We may throttle, contact you, or apply a hard limit if usage
              materially exceeds typical interactive patterns; we&apos;ll give notice first.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">4. Service availability</h2>
            <p>
              We aim for high availability but do not guarantee uninterrupted access. Scheduled
              maintenance may occur from time to time.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">5. Termination</h2>
            <p>
              You may close your account at any time. We may suspend or terminate accounts that
              violate these terms.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">6. Contact</h2>
            <p>
              Questions? Reach us at{" "}
              <EmailLink email={CONTACT_EMAIL} className="text-zinc-900 underline" />
              .
            </p>
          </section>
        </div>
      </article>
    </>
  );
}
