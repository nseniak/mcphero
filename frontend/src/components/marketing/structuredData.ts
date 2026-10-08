const SITE_URL = (import.meta.env.VITE_SITE_URL as string | undefined) ?? "https://mcphero.io";
const SITE_NAME = "MCP Hero";
const LOGO_URL = `${SITE_URL}/hero-logo.svg`;
const PUBLISHER_NAME = "Nitsan Seniak";

export const organizationSchema = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: SITE_NAME,
  url: SITE_URL,
  logo: LOGO_URL,
  publisher: {
    "@type": "Person",
    name: PUBLISHER_NAME,
  },
} as const;

export const softwareApplicationSchema = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: SITE_NAME,
  url: SITE_URL,
  applicationCategory: "BusinessApplication",
  operatingSystem: "Web",
  description:
    "MCP Hero aggregates upstream MCP servers behind a single endpoint with role-based access, OAuth, and audit logging.",
  publisher: {
    "@type": "Person",
    name: PUBLISHER_NAME,
  },
  offers: {
    "@type": "AggregateOffer",
    priceCurrency: "USD",
    lowPrice: "0",
    highPrice: "19",
    offerCount: "2",
  },
} as const;

export const productSchema = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: SITE_NAME,
  url: `${SITE_URL}/pricing`,
  description:
    "MCP Hero pricing: a Free plan, and a Team plan at $19/month for up to 10 teammates.",
  brand: {
    "@type": "Brand",
    name: SITE_NAME,
  },
  offers: [
    {
      "@type": "Offer",
      name: "Free",
      price: "0",
      priceCurrency: "USD",
      url: `${SITE_URL}/pricing`,
      description:
        "Up to 3 teammates, 5 remote HTTP MCPs and 1 hosted stdio MCP. 30-day audit log.",
    },
    {
      "@type": "Offer",
      name: "Team",
      price: "19",
      priceCurrency: "USD",
      url: `${SITE_URL}/pricing`,
      priceSpecification: {
        "@type": "UnitPriceSpecification",
        price: "19",
        priceCurrency: "USD",
        unitText: "month",
      },
      description:
        "Up to 10 teammates included, $5 per seat per month above 10. Unlimited MCPs, custom roles, MCP tool argument checks, 1-year audit retention.",
    },
  ],
} as const;
