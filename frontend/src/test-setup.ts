import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// RTL doesn't auto-clean between tests under vitest the way it does
// under jest. Without this, leftover DOM from the previous test makes
// queries like getByRole match multiple elements across renders.
afterEach(() => {
  cleanup();
});
