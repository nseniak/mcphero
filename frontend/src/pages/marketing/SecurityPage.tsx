import { EmailLink } from "../../components/marketing/EmailLink";
import { Seo } from "../../components/marketing/Seo";
import { organizationSchema } from "../../components/marketing/structuredData";

const SECURITY_EMAIL =
  (import.meta.env.VITE_SECURITY_EMAIL as string | undefined) ?? "security@mcphero.io";

export function SecurityPage() {
  return (
    <>
      <Seo
        title="Security"
        description="MCP Hero's security posture: isolation, encryption, authentication, and disclosure."
        path="/security"
        jsonLd={organizationSchema}
      />
      <article className="px-6 py-16 max-w-3xl mx-auto prose prose-zinc prose-sm md:prose-base">
        <h1 className="text-3xl md:text-4xl font-semibold text-zinc-900 mb-2">Security</h1>
        <p className="text-sm text-zinc-500 mb-10">Last updated: 2026-05-09</p>

        <div className="space-y-6 text-zinc-700 leading-relaxed">
          <section className="space-y-3">
            <p>
              MCP Hero sits between AI assistants and the MCP servers you trust,
              which puts us on the path of credentials, tool calls, and
              third-party data. The design assumes a hostile network and a
              hostile MCP server. This page summarizes the controls we rely on.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">Isolation</h2>
            <p>
              Every MCP server you connect runs in its own ephemeral cloud
              sandbox. Sandboxes are provisioned per session via{" "}
              <a
                href="https://e2b.dev"
                target="_blank"
                rel="noopener noreferrer"
                className="font-semibold text-zinc-900 underline decoration-zinc-300 underline-offset-2 hover:decoration-zinc-900"
              >
                E2B
              </a>
              , a specialist sandbox provider for AI workloads, and torn down
              when the session ends. They have no
              shared filesystem and no persistent state, and they cannot reach
              the gateway host or other tenants' sandboxes. A misbehaving or
              compromised MCP server is contained to its own session.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">Authentication and tenancy</h2>
            <p>
              Dashboard access uses Google OAuth; we never see or store a
              password. Every account belongs to an organization, and all
              data — MCP servers, variables, OAuth tokens, logs — is scoped to
              that organization. Role-based access controls which tools each
              member can invoke. Cross-organization data access is not a code
              path; it is an absent one.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">Encryption at rest</h2>
            <p>
              Every sensitive value stored in our database is encrypted with{" "}
              <strong>AES-256-GCM</strong>: upstream OAuth
              access and refresh tokens, OAuth client secrets, password-typed
              variables, and any credential or config files you upload to a
              server. The encryption key is derived from a deployment-wide
              root secret via <strong>HKDF</strong>, with a fresh random nonce
              per record so identical plaintexts never produce identical
              ciphertexts. The root secret lives only in the deployment's
              environment; the gateway refuses to start without it, and a
              database snapshot taken without it cannot be decrypted.
              Plaintext credentials never touch disk.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">Encryption in flight</h2>
            <p>
              All public traffic terminates on TLS at the edge. Plaintext
              credentials exist only briefly in process memory: the gateway
              decrypts them at session start, hands them to the sandbox, and
              discards them. They are never returned by any API — list and
              detail endpoints expose only metadata (name, last-4 preview for
              passwords, timestamps).
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">Logging</h2>
            <p>
              Logs never contain raw credentials. A static check in CI fails
              the build if any log call is wired to receive raw environment,
              header, or variable values, and any password value an MCP server
              echoes to its own stderr is replaced with{" "}
              <code>[REDACTED:NAME]</code> before it reaches the operator's
              log buffer.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">Deletion</h2>
            <p>
              When you delete an MCP server, its variables, tokens, uploaded
              files, and logs are deleted with it. Deleting an organization
              removes the same data for every member. Encrypted backups exist
              for disaster recovery and roll over on a fixed retention window;
              after that, deleted data is unrecoverable.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-xl font-semibold text-zinc-900">Reporting an issue</h2>
            <p>
              If you find a security issue, please email{" "}
              <EmailLink email={SECURITY_EMAIL} className="text-zinc-900 underline" />.
              We will acknowledge your report and keep you posted as we
              investigate and fix.
            </p>
          </section>
        </div>
      </article>
    </>
  );
}
