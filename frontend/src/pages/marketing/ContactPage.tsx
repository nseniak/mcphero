import { Lock, Mail, ShieldCheck } from "lucide-react";
import { Link, useLocation } from "react-router";
import { EmailLink } from "../../components/marketing/EmailLink";
import { SectionHeading } from "../../components/marketing/SectionHeading";
import { Seo } from "../../components/marketing/Seo";
import { organizationSchema } from "../../components/marketing/structuredData";
import { track } from "../../lib/analytics";

const CONTACT_EMAIL =
  (import.meta.env.VITE_CONTACT_EMAIL as string | undefined) ?? "hello@mcphero.io";
const SECURITY_EMAIL =
  (import.meta.env.VITE_SECURITY_EMAIL as string | undefined) ?? "security@mcphero.io";
const PRIVACY_EMAIL =
  (import.meta.env.VITE_PRIVACY_EMAIL as string | undefined) ?? "privacy@mcphero.io";

interface ContactCardProps {
  icon: React.ReactNode;
  title: string;
  description: React.ReactNode;
  email: string;
  ctaId: string;
  page: string;
}

function ContactCard({ icon, title, description, email, ctaId, page }: ContactCardProps) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-6 space-y-3 flex flex-col">
      <div className="text-zinc-900">{icon}</div>
      <h3 className="text-base font-semibold text-zinc-900">{title}</h3>
      <p className="text-sm text-zinc-600 leading-relaxed flex-1">{description}</p>
      <EmailLink
        email={email}
        onClick={() => track("landing_cta_clicked", { cta_id: ctaId, page })}
        className="text-sm text-zinc-900 underline underline-offset-2 hover:text-zinc-700 transition-colors"
      />
    </div>
  );
}

export function ContactPage() {
  const { pathname } = useLocation();
  return (
    <>
      <Seo
        title="Contact"
        description="Get in touch with MCP Hero — general questions, security disclosures, and privacy or data requests."
        path="/contact"
        jsonLd={organizationSchema}
      />

      <section className="px-6 pt-20 pb-10 md:pt-24">
        <div className="max-w-4xl mx-auto text-center space-y-4">
          <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">Contact</p>
          <h1 className="text-4xl md:text-5xl font-semibold text-zinc-900 tracking-tight">
            Get in touch.
          </h1>
          <p className="text-lg text-zinc-600 max-w-2xl mx-auto">
            Questions about MCP Hero, feedback on what's missing, or anything else — we read every
            email.
          </p>
        </div>
      </section>

      <section className="px-6 py-16 md:py-20 bg-zinc-50 border-y border-zinc-200">
        <div className="max-w-6xl mx-auto space-y-12">
          <SectionHeading
            eyebrow="How to reach us"
            title="Pick the channel that fits."
          />
          <div className="grid md:grid-cols-3 gap-4">
            <ContactCard
              icon={<Mail className="h-5 w-5" />}
              title="General"
              description="Sales questions, partnerships, product feedback, or just saying hi. We usually reply within a business day."
              email={CONTACT_EMAIL}
              ctaId="contact_email_general"
              page={pathname}
            />
            <ContactCard
              icon={<ShieldCheck className="h-5 w-5" />}
              title="Security"
              description={
                <>
                  Found a vulnerability? Please email us so we can fix it
                  before it's public. See our{" "}
                  <Link to="/security" className="text-zinc-900 underline underline-offset-2">
                    security page
                  </Link>{" "}
                  for the full posture.
                </>
              }
              email={SECURITY_EMAIL}
              ctaId="contact_email_security"
              page={pathname}
            />
            <ContactCard
              icon={<Lock className="h-5 w-5" />}
              title="Privacy & data requests"
              description={
                <>
                  Access, export, or deletion requests under GDPR. Our{" "}
                  <Link to="/privacy" className="text-zinc-900 underline underline-offset-2">
                    privacy policy
                  </Link>{" "}
                  describes what we collect and why.
                </>
              }
              email={PRIVACY_EMAIL}
              ctaId="contact_email_privacy"
              page={pathname}
            />
          </div>
          <p className="text-sm text-zinc-500 text-center">
            MCP Hero is operated by Nitsan Seniak.
          </p>
        </div>
      </section>
    </>
  );
}
