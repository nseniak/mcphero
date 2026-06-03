import { Helmet } from "react-helmet-async";

type JsonLdValue = Record<string, unknown>;

interface SeoProps {
  title: string;
  description: string;
  path: string;
  image?: string;
  jsonLd?: JsonLdValue | JsonLdValue[];
}

const SITE_NAME = "MCP Hero";
const SITE_URL = (import.meta.env.VITE_SITE_URL as string | undefined) ?? "https://mcphero.io";
const DEFAULT_OG_IMAGE = `${SITE_URL}/og-image.png`;

export function Seo({ title, description, path, image, jsonLd }: SeoProps) {
  const fullTitle = title === SITE_NAME ? title : `${title} — ${SITE_NAME}`;
  const url = `${SITE_URL}${path}`;
  const ogImage = image ?? DEFAULT_OG_IMAGE;
  const schemas = jsonLd === undefined ? [] : Array.isArray(jsonLd) ? jsonLd : [jsonLd];
  return (
    <Helmet>
      <title>{fullTitle}</title>
      <meta name="description" content={description} />
      <link rel="canonical" href={url} />
      <meta property="og:type" content="website" />
      <meta property="og:site_name" content={SITE_NAME} />
      <meta property="og:title" content={fullTitle} />
      <meta property="og:description" content={description} />
      <meta property="og:url" content={url} />
      <meta property="og:image" content={ogImage} />
      <meta name="twitter:card" content="summary_large_image" />
      <meta name="twitter:title" content={fullTitle} />
      <meta name="twitter:description" content={description} />
      <meta name="twitter:image" content={ogImage} />
      {schemas.map((schema, index) => (
        <script key={index} type="application/ld+json">
          {JSON.stringify(schema)}
        </script>
      ))}
    </Helmet>
  );
}
