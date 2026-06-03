import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Standalone vitest config to keep the test pipeline isolated from the
// build pipeline. The build config (vite.config.ts) wires Tailwind, the
// prerenderer, and dev-server proxies — none of which the unit tests
// need. Re-exporting from the build config dragged the rolldown plugin
// resolution into the test runner; this config sidesteps that by
// declaring only the bits vitest needs.
//
// jsdom is required by React Testing Library for component tests
// (SecretsManager and friends). Pure helper tests don't need a DOM
// but the cost of running them under jsdom is negligible.
export default defineConfig({
  plugins: [react()],
  // Mirror vite.config.ts's fs.allow so vitest can transform repo-root
  // assets reached via ?raw imports (e.g. DocsPage's `../../../../docs/*.md`
  // glob). vite 8.0.5+ tightened fs.deny enforcement for queries; without
  // this allowlist the doc transforms fail with "Denied ID …?raw".
  server: {
    fs: {
      allow: [".", ".."],
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    setupFiles: ["src/test-setup.ts"],
    globals: false,
  },
});
