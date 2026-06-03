/**
 * Coverage for the cross-reference helpers used by TemplateVarsManager
 * and JsonConfigEditor: `findReferences` walks env / headers across
 * both the wrapped (`mcpServers: {...}`) and single-server JSON
 * shapes, and `analyseReferences` produces the badge counts +
 * unresolved list.
 */
import { describe, expect, it } from "vitest";

import {
  analyseReferences,
  findReferences,
  findReferencesInSandboxFiles,
  resolvedNamesFromAnalysis,
} from "./template-var-references";

describe("findReferences", () => {
  it("walks env values in a wrapped mcpServers object", () => {
    const refs = findReferences({
      mcpServers: {
        github: {
          command: "npx",
          args: ["-y", "anything"],
          env: { GH: "${GH_TOKEN}", OTHER: "no-placeholder" },
        },
      },
    });
    expect(refs).toEqual([
      {
        name: "GH_TOKEN",
        location: { mcpId: "github", field: "env", jsonKey: "GH" },
      },
    ]);
  });

  it("walks header values in a wrapped mcpServers object", () => {
    const refs = findReferences({
      mcpServers: {
        weather: {
          url: "https://example.test/mcp",
          headers: {
            Authorization: "Bearer ${API_KEY}",
            "X-Static": "no-secret",
          },
        },
      },
    });
    expect(refs).toEqual([
      {
        name: "API_KEY",
        location: { mcpId: "weather", field: "headers", jsonKey: "Authorization" },
      },
    ]);
  });

  it("accepts the single-server form (no mcpServers wrapper)", () => {
    const refs = findReferences({
      command: "npx",
      env: { GH: "${GH_TOKEN}" },
    });
    expect(refs).toEqual([
      {
        name: "GH_TOKEN",
        location: { mcpId: "", field: "env", jsonKey: "GH" },
      },
    ]);
  });

  it("accepts the single-id wrapper shape ({<id>: {command, env}})", () => {
    // The shape the create-wizard's JSON box accepts most commonly:
    // user pastes ``{"slack": {"command": "npx", "env": {...}}}``
    // without the explicit ``mcpServers`` wrapper.
    const refs = findReferences({
      slack: {
        command: "npx",
        args: ["-y", "anything"],
        env: { SLACK_TOKEN: "${MY_SECRET}" },
      },
    });
    expect(refs).toEqual([
      {
        name: "MY_SECRET",
        location: { mcpId: "slack", field: "env", jsonKey: "SLACK_TOKEN" },
      },
    ]);
  });

  it("walks every top-level key whose value looks like a server entry", () => {
    const refs = findReferences({
      slack: { command: "npx", env: { A: "${X}" } },
      github: { url: "https://example.test/mcp", headers: { H: "${Y}" } },
      // Garbage neighbour — ignored because it's not a server entry.
      version: 1,
    });
    expect(refs.map((r) => r.name).sort()).toEqual(["X", "Y"]);
  });

  it("returns multiple references including duplicates", () => {
    const refs = findReferences({
      command: "npx",
      env: { A: "${X}", B: "${X}-${Y}" },
    });
    expect(refs).toHaveLength(3);
    expect(refs.map((r) => r.name).sort()).toEqual(["X", "X", "Y"]);
  });

  it("ignores invalid placeholder names (lowercase, spaces)", () => {
    const refs = findReferences({
      command: "npx",
      env: { A: "${lowercase}", B: "${has spaces}", C: "${VALID}" },
    });
    expect(refs.map((r) => r.name)).toEqual(["VALID"]);
  });

  it("returns [] for non-object input", () => {
    expect(findReferences(null)).toEqual([]);
    expect(findReferences(undefined)).toEqual([]);
    expect(findReferences(42)).toEqual([]);
    expect(findReferences("string")).toEqual([]);
  });

  it("handles empty objects gracefully", () => {
    expect(findReferences({})).toEqual([]);
    expect(findReferences({ mcpServers: {} })).toEqual([]);
  });

  it("walks command, args elements, and url too", () => {
    const refs = findReferences({
      command: "${PYTHON_BIN}",
      args: ["-c", "print(${SOME_VAR});"],
      url: "https://example.test/${ORG}/mcp",
    });
    const names = refs.map((r) => r.name).sort();
    expect(names).toEqual(["ORG", "PYTHON_BIN", "SOME_VAR"]);
    const fields = refs.map((r) => r.location.field).sort();
    expect(fields).toEqual(["args", "command", "url"]);
  });

  it("ignores escaped \\${NAME} placeholders in any field", () => {
    const refs = findReferences({
      command: "python3",
      args: ["-c", "import os; print(os.environ['\\${HOST_VAR}'])"],
      env: { LITERAL: "\\${KEEP_ME}" },
    });
    expect(refs).toEqual([]);
  });

  it("expands real placeholders even when an escape sits next to them", () => {
    const refs = findReferences({
      command: "python3",
      args: ["-c", "${REAL}-\\${LITERAL}"],
    });
    expect(refs).toHaveLength(1);
    expect(refs[0].name).toBe("REAL");
  });
});

describe("analyseReferences", () => {
  it("counts each reference per name", () => {
    const refs = findReferences({
      command: "npx",
      env: { A: "${X}", B: "${X}-${Y}" },
    });
    const analysis = analyseReferences(refs, ["X"]);
    expect(analysis.byName.get("X")).toEqual({ count: 2, defined: true });
    expect(analysis.byName.get("Y")).toEqual({ count: 1, defined: false });
  });

  it("flags unresolved references", () => {
    const refs = findReferences({
      env: { A: "${KNOWN}", B: "${UNKNOWN}" },
    });
    const analysis = analyseReferences(refs, ["KNOWN"]);
    expect(analysis.unresolved).toHaveLength(1);
    expect(analysis.unresolved[0].name).toBe("UNKNOWN");
  });

  it("includes defined-but-unreferenced secrets with count 0", () => {
    const refs: ReturnType<typeof findReferences> = [];
    const analysis = analyseReferences(refs, ["A", "B"]);
    expect(analysis.byName.get("A")).toEqual({ count: 0, defined: true });
    expect(analysis.byName.get("B")).toEqual({ count: 0, defined: true });
    expect(analysis.unresolved).toEqual([]);
  });

  it("treats unresolved-with-multiple-refs as multiple unresolved entries", () => {
    const refs = findReferences({
      env: { A: "${MISSING}", B: "${MISSING}" },
    });
    const analysis = analyseReferences(refs, []);
    expect(analysis.unresolved).toHaveLength(2);
    expect(analysis.byName.get("MISSING")).toEqual({ count: 2, defined: false });
  });
});

describe("findReferencesInSandboxFiles", () => {
  it("emits a reference per ${...} token in a target_path", () => {
    const refs = findReferencesInSandboxFiles([
      {
        name: "GCP_CRED",
        target_path: "${HOME}/.config/gcloud/credentials.json",
      },
    ]);
    expect(refs).toEqual([
      {
        name: "HOME",
        location: { mcpId: "", field: "target_path", jsonKey: "GCP_CRED" },
      },
    ]);
  });

  it("emits multiple references when target_path mixes vars", () => {
    const refs = findReferencesInSandboxFiles([
      {
        name: "TENANT_CRED",
        target_path: "${HOME}/.config/myapp/${TENANT_ID}/creds.json",
      },
    ]);
    expect(refs.map((r) => r.name).sort()).toEqual(["HOME", "TENANT_ID"]);
    for (const r of refs) {
      expect(r.location.field).toBe("target_path");
      expect(r.location.jsonKey).toBe("TENANT_CRED");
    }
  });

  it("walks every file in the list", () => {
    const refs = findReferencesInSandboxFiles([
      { name: "A", target_path: "${ALPHA}/a" },
      { name: "B", target_path: "${BETA}/b" },
      { name: "C", target_path: "/literal/c" },
    ]);
    const byJsonKey = new Map(
      refs.map((r) => [r.location.jsonKey, r.name]),
    );
    expect(byJsonKey.get("A")).toBe("ALPHA");
    expect(byJsonKey.get("B")).toBe("BETA");
    expect(byJsonKey.has("C")).toBe(false);
  });

  it("respects the \\${NAME} escape", () => {
    const refs = findReferencesInSandboxFiles([
      { name: "ESCAPED", target_path: "/literal/\\${NOT_A_VAR}/x" },
    ]);
    expect(refs).toEqual([]);
  });

  it("returns [] for an empty list", () => {
    expect(findReferencesInSandboxFiles([])).toEqual([]);
  });

  it("composes with analyseReferences for the unresolved-callout flow", () => {
    // The detail page concatenates findReferences(json) with
    // findReferencesInSandboxFiles(files); analyseReferences runs
    // against the union and surfaces undefined names from either
    // source in the same callout.
    const configRefs = findReferences({
      command: "node",
      env: { TOKEN: "${API_TOKEN}" },
    });
    const fileRefs = findReferencesInSandboxFiles([
      { name: "PROFILE_CFG", target_path: "${HOME}/.aws/config-${PROFILE}" },
    ]);
    const analysis = analyseReferences(
      [...configRefs, ...fileRefs],
      ["HOME"],
    );
    expect(analysis.unresolved.map((r) => r.name).sort()).toEqual([
      "API_TOKEN",
      "PROFILE",
    ]);
    expect(analysis.byName.get("API_TOKEN")).toEqual({
      count: 1,
      defined: false,
    });
    expect(analysis.byName.get("PROFILE")).toEqual({
      count: 1,
      defined: false,
    });
  });
});

describe("resolvedNamesFromAnalysis", () => {
  it("returns only names where defined=true", () => {
    const refs = findReferences({
      env: { A: "${KNOWN}", B: "${UNKNOWN}" },
    });
    const analysis = analyseReferences(refs, ["KNOWN", "UNREFERENCED"]);
    const resolved = resolvedNamesFromAnalysis(analysis);
    expect(resolved.has("KNOWN")).toBe(true);
    expect(resolved.has("UNREFERENCED")).toBe(true);
    expect(resolved.has("UNKNOWN")).toBe(false);
  });
});
