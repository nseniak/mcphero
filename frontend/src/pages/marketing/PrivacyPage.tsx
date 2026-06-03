import { EmailLink } from "../../components/marketing/EmailLink";
import { Seo } from "../../components/marketing/Seo";
import { organizationSchema } from "../../components/marketing/structuredData";

const PRIVACY_EMAIL =
  (import.meta.env.VITE_PRIVACY_EMAIL as string | undefined) ?? "privacy@mcphero.io";

export function PrivacyPage() {
  return (
    <>
      <Seo
        title="Privacy Policy"
        description="How MCP Hero collects, uses, and protects your data."
        path="/privacy"
        jsonLd={organizationSchema}
      />
      <article className="px-6 py-16 max-w-3xl mx-auto prose prose-zinc prose-sm md:prose-base">
        <h1 className="text-3xl md:text-4xl font-semibold text-zinc-900 mb-2">Privacy Policy</h1>
        <p className="text-sm text-zinc-500 mb-10">Last updated: 2026-05-01</p>

        <div className="space-y-6 text-zinc-700 leading-relaxed">
          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">1. Data controller</h2>
            <p>
              MCP Hero is operated by Nitsan Seniak, acting as the data controller for the
              personal data processed through the service. For privacy or data-protection
              requests, contact{" "}
              <EmailLink email={PRIVACY_EMAIL} className="text-zinc-900 underline" />
              .
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">2. What we collect</h2>
            <p>
              MCP Hero collects the minimum data needed to operate the service: your email address
              (for authentication), your team memberships and permissions, and an audit log of
              tool invocations made through the gateway.
            </p>
            <p>
              <strong>We do not retain your company's data.</strong> The audit log records only
              metadata about which tools were invoked — user, tool name, time, outcome — never the
              arguments passed to a tool or the results returned. We never see or store the
              contents of your connected apps.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">3. How we use it</h2>
            <p>
              We use this data to authenticate you, enforce access policies, display your audit
              log, and operate the service. We do not sell your data.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">4. Third parties</h2>
            <p>
              We use Google OAuth for sign-in, Sentry for error reporting, and Mixpanel for product
              analytics. Each receives only the data needed for its purpose.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">5. Data retention</h2>
            <p>
              Audit log entries are retained for 7 days, then automatically deleted. OAuth refresh
              tokens are encrypted at rest and deleted when you disconnect.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">6. Contact</h2>
            <p>
              Questions about this policy? Reach us at{" "}
              <EmailLink email={PRIVACY_EMAIL} className="text-zinc-900 underline" />
              .
            </p>
          </section>
        </div>
      </article>
    </>
  );
}
