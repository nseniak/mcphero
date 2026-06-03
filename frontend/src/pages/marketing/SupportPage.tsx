import { EmailLink } from "../../components/marketing/EmailLink";
import { Seo } from "../../components/marketing/Seo";
import { organizationSchema } from "../../components/marketing/structuredData";

const SUPPORT_EMAIL =
  (import.meta.env.VITE_SUPPORT_EMAIL as string | undefined) ?? "support@mcphero.io";

export function SupportPage() {
  return (
    <>
      <Seo
        title="Support"
        description="Get help with MCP Hero — reach support by email."
        path="/support"
        jsonLd={organizationSchema}
      />
      <article className="px-6 py-16 max-w-3xl mx-auto prose prose-zinc prose-sm md:prose-base">
        <h1 className="text-3xl md:text-4xl font-semibold text-zinc-900 mb-2">Support</h1>

        <div className="space-y-6 text-zinc-700 leading-relaxed">
          <section className="space-y-3">
            <p>
              Need a hand? Email{" "}
              <EmailLink email={SUPPORT_EMAIL} className="text-zinc-900 underline" />{" "}
              and we'll get back to you.
            </p>
          </section>
        </div>
      </article>
    </>
  );
}
