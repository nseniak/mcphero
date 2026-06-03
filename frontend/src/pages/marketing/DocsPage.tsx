import { useEffect } from "react";
import { Link, NavLink, useLocation, useParams } from "react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import { Seo } from "../../components/marketing/Seo";

// Bundle every doc at build time. Vite resolves relative paths from this
// file: `../../../../docs/` lands at the repo root's `docs/` directory.
// `server.fs.allow` is widened in vite.config.ts so this works in dev too.
const DOC_MODULES = import.meta.glob("../../../../docs/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Map "<some>/.../docs/concepts.md" → "concepts".
function pathToSlug(path: string): string {
  const filename = path.split("/").pop() ?? "";
  return filename.replace(/\.md$/, "").toLowerCase();
}

const DOCS: Record<string, string> = {};
for (const [path, content] of Object.entries(DOC_MODULES)) {
  DOCS[pathToSlug(path)] = content;
}

// Sidebar order. The keys match URL slugs; "readme" is special and renders
// as the /docs index.
type DocEntry = { slug: string; title: string };
export const SIDEBAR: DocEntry[] = [
  { slug: "concepts", title: "Concepts" },
  { slug: "getting-started", title: "Getting started" },
  { slug: "upstream-mcps", title: "Upstream MCPs" },
  { slug: "stdio-authent", title: "Stdio MCP authentication" },
  { slug: "your-own-mcp-code", title: "Running your own MCP code" },
  { slug: "team", title: "Team" },
  { slug: "roles-and-permissions", title: "Roles and permissions" },
  { slug: "audit-log", title: "Audit log" },
  { slug: "admin-mcp", title: "Admin MCP" },
];

// Strip the first heading from the markdown so we can render it as the page
// title separately (and avoid duplicating it inside the prose container).
function splitFirstHeading(md: string): { title: string | null; body: string } {
  const match = md.match(/^#\s+(.+?)\s*\n+/);
  if (!match) return { title: null, body: md };
  return { title: match[1], body: md.slice(match[0].length) };
}

// Extract the first prose paragraph from a doc body, used as the meta
// description and OG description. Skips headings, blockquotes/callouts,
// list items, and code fences. Strips inline Markdown (links, bold,
// italics, code) so it reads as plain text in search results. Truncates
// to ~155 chars (Google's typical snippet cutoff).
function extractDescription(body: string, maxChars = 155): string {
  const lines = body.split("\n");
  let inFence = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith("```")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    if (!line) continue;
    if (line.startsWith("#")) continue;
    if (line.startsWith(">")) continue;
    if (/^[-*+]\s/.test(line)) continue;
    if (/^\d+\.\s/.test(line)) continue;
    const stripped = line
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/\*([^*]+)\*/g, "$1")
      .replace(/_([^_]+)_/g, "$1")
      .trim();
    if (!stripped) continue;
    if (stripped.length <= maxChars) return stripped;
    return stripped.slice(0, maxChars - 1).trimEnd() + "…";
  }
  return "";
}

// Rewrite intra-doc Markdown links so `[x](concepts.md)` and
// `[x](concepts.md#anchor)` route to `/docs/concepts(#anchor)` instead of
// 404'ing at `/docs/concepts.md`. External and absolute links pass through.
function rewriteHref(href: string | undefined): string | undefined {
  if (!href) return href;
  if (/^[a-z]+:\/\//i.test(href) || href.startsWith("#") || href.startsWith("/")) {
    return href;
  }
  const match = href.match(/^([^#?]+)(.*)$/);
  if (!match) return href;
  const [, target, rest] = match;
  if (!target.endsWith(".md")) return href;
  const slug = target.replace(/\.md$/, "").toLowerCase();
  return `/docs/${slug}${rest}`;
}

// Default SEO description for the /docs index. The README's prose works
// out of the box, but we keep an explicit fallback for when the README is
// edited and the auto-extractor finds nothing usable.
const INDEX_FALLBACK_DESCRIPTION =
  "Admin documentation for MCP Hero: setting up your organization, adding upstream MCPs, managing roles and permissions, and using the audit log.";

export function DocsPage() {
  const { slug } = useParams<{ slug?: string }>();
  // Accept both `/docs/foo` and `/docs/foo.md` — external links from
  // outside the docs (e.g. the upstream-create wizard's "Learn more"
  // CTA) point at the `.md`-suffixed form because that's the file
  // name on disk. The DOCS map is keyed without the extension.
  const activeSlug = (slug ?? "readme").replace(/\.md$/i, "").toLowerCase();
  const raw = DOCS[activeSlug] ?? null;
  const path = slug ? `/docs/${activeSlug}` : "/docs";

  const { hash } = useLocation();

  // Scroll behaviour on doc change:
  // - If the URL carries a ``#anchor`` (e.g. external deep-link from
  //   the upstream-create wizard's "Learn more" CTA), scroll the
  //   matching element into view.
  // - Otherwise, reset to top (default browser behaviour is to keep
  //   the scroll position, which is jarring between docs).
  // The lookup is deferred a tick so it runs after ReactMarkdown has
  // committed the rendered DOM the anchor lives in.
  useEffect(() => {
    if (hash) {
      const id = decodeURIComponent(hash.slice(1));
      const target = document.getElementById(id);
      if (target) {
        target.scrollIntoView({ behavior: "auto", block: "start" });
        return;
      }
    }
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [activeSlug, hash]);

  const { title, body } = raw ? splitFirstHeading(raw) : { title: null, body: "" };
  const seoTitle = raw ? (title ?? "Admin docs") : "Page not found";
  const seoDescription = raw
    ? (extractDescription(body) ||
       (activeSlug === "readme" ? INDEX_FALLBACK_DESCRIPTION : `${title ?? activeSlug} — MCP Hero admin documentation.`))
    : "The doc page you requested doesn't exist.";

  return (
    <>
      <Seo title={seoTitle} description={seoDescription} path={path} />
      <div className="max-w-6xl mx-auto px-6 py-10 grid grid-cols-1 md:grid-cols-[14rem_1fr] gap-10">
        <aside className="md:sticky md:top-20 md:self-start">
        <p className="text-xs font-semibold text-zinc-500 uppercase tracking-wide mb-3">
          Admin docs
        </p>
        <nav className="flex flex-col gap-1 text-sm">
          <NavLink
            to="/docs"
            end
            className={({ isActive }) =>
              `px-2 py-1 rounded ${
                isActive
                  ? "bg-zinc-100 text-zinc-900 font-medium"
                  : "text-zinc-600 hover:text-zinc-900 hover:bg-zinc-50"
              }`
            }
          >
            Overview
          </NavLink>
          {SIDEBAR.map((entry) => (
            <NavLink
              key={entry.slug}
              to={`/docs/${entry.slug}`}
              className={({ isActive }) =>
                `px-2 py-1 rounded ${
                  isActive
                    ? "bg-zinc-100 text-zinc-900 font-medium"
                    : "text-zinc-600 hover:text-zinc-900 hover:bg-zinc-50"
                }`
              }
            >
              {entry.title}
            </NavLink>
          ))}
        </nav>
      </aside>

      <article className="min-w-0">
        {raw === null ? (
          <div className="prose prose-zinc max-w-none">
            <h1>Page not found</h1>
            <p>
              The doc <code>{activeSlug}</code> doesn't exist.{" "}
              <Link to="/docs" className="text-blue-600 hover:underline">
                Back to overview
              </Link>
              .
            </p>
          </div>
        ) : (
          <div className="prose prose-zinc max-w-none prose-headings:scroll-mt-20 prose-pre:bg-zinc-900 prose-pre:text-zinc-100 prose-code:before:hidden prose-code:after:hidden prose-code:bg-zinc-100 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:font-normal prose-a:text-blue-600 hover:prose-a:underline [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-inherit [&_pre_code]:rounded-none">
            {title && <h1>{title}</h1>}
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              // ``rehype-raw`` lets us keep explicit ``<a id="...">``
              // anchor tags inside the markdown source so external
              // deep-links (e.g. the upstream-create wizard's "Learn
              // more" CTA) can target named subsections without
              // relying on auto-generated heading ids. Safe because
              // every doc here is in-repo content we control.
              rehypePlugins={[rehypeRaw]}
              components={{
                a: ({ href, children, ...props }) => {
                  const rewritten = rewriteHref(href);
                  const isExternal = rewritten?.match(/^[a-z]+:\/\//i);
                  if (isExternal) {
                    return (
                      <a href={rewritten} target="_blank" rel="noopener noreferrer" {...props}>
                        {children}
                      </a>
                    );
                  }
                  return (
                    <Link to={rewritten ?? "#"} {...(props as Record<string, unknown>)}>
                      {children}
                    </Link>
                  );
                },
              }}
            >
              {body}
            </ReactMarkdown>
          </div>
        )}
      </article>
    </div>
    </>
  );
}
