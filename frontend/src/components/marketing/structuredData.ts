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
    highPrice: "4.99",
    offerCount: "2",
  },
} as const;

export const productSchema = {
  "@context": "https://schema.org",
  "@type": "Product",
  name: SITE_NAME,
  url: `${SITE_URL}/pricing`,
  description:
    "MCP Hero pricing — free for Remote HTTP MCPs, paid plan for Hosted stdio MCPs.",
  brand: {
    "@type": "Brand",
    name: SITE_NAME,
  },
  offers: [
    {
      "@type": "Offer",
      name: "Remote HTTP MCPs",
      price: "0",
      priceCurrency: "USD",
      url: `${SITE_URL}/pricing`,
      description:
        "MCPs you connect by URL. The vendor hosts them; your team just signs in.",
    },
    {
      "@type": "Offer",
      name: "Hosted stdio MCPs",
      price: "4.99",
      priceCurrency: "USD",
      url: `${SITE_URL}/pricing`,
      priceSpecification: {
        "@type": "UnitPriceSpecification",
        price: "4.99",
        priceCurrency: "USD",
        unitText: "user/month",
      },
      description:
        "MCPs you'd normally run as an executable on your machine. We run them for you — no infrastructure to manage.",
    },
  ],
} as const;
