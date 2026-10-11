import ts from "typescript";
import { describe, expect, it } from "vitest";

import { generateCpTypes } from "../../../scripts/generate-cp-types";

function document() {
  return {
    openapi: "3.1.0", info: { title: "Controlled command contract", version: "1" }, paths: {},
    "x-sidera-command-payloads": { ReviewedCommand: { $ref: "#/components/schemas/ReviewedInput" } },
    components: { schemas: {
      ReviewedInput: {
        type: "object", required: ["original_pin"], properties: {
          optional_parent: { type: ["string", "null"], default: null },
          choices: { type: "array", items: { type: "string" }, default: [] },
          "choice/mode~pin": { type: "string", default: "inherit" },
          original_pin: { type: "string", default: "pinned" },
        },
      },
      ReadResponse: {
        type: "object", properties: { choices: { type: "array", items: { type: "string" }, default: [] } },
      },
    } },
  };
}

function properties(source: string, component: string): ts.PropertySignature[] {
  const file = ts.createSourceFile("controlled.d.ts", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const result: ts.PropertySignature[] = [];
  function visit(node: ts.Node) {
    if (ts.isPropertySignature(node) && (ts.isIdentifier(node.name) || ts.isStringLiteral(node.name))
      && node.name.text === component && node.type && ts.isTypeLiteralNode(node.type)) {
      for (const member of node.type.members) if (ts.isPropertySignature(member)) result.push(member);
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  return result;
}

function field(items: ts.PropertySignature[], name: string) {
  const property = items.find(item => (ts.isIdentifier(item.name) || ts.isStringLiteral(item.name)) && item.name.text === name);
  if (property === undefined) throw new Error("Missing generated test property");
  return property;
}

describe("owning command input requiredness", () => {
  it("allows mapped optional defaults and escaped property keys to remain omitted", async () => {
    const generated = properties(await generateCpTypes(document()), "ReviewedInput");
    expect(field(generated, "optional_parent").questionToken).toBeDefined();
    expect(field(generated, "choices").questionToken).toBeDefined();
    expect(field(generated, "choice/mode~pin").questionToken).toBeDefined();
  });

  it("keeps an actual required mapped pin required even when it has a default", async () => {
    const generated = properties(await generateCpTypes(document()), "ReviewedInput");
    expect(field(generated, "original_pin").questionToken).toBeUndefined();
  });

  it("preserves unmapped response defaults including the same property name", async () => {
    const generated = properties(await generateCpTypes(document()), "ReadResponse");
    expect(field(generated, "choices").questionToken).toBeUndefined();
  });

  it("rejects a missing mapped component without a generated output", async () => {
    const invalid = document();
    invalid["x-sidera-command-payloads"].ReviewedCommand.$ref = "#/components/schemas/MissingInput";
    await expect(generateCpTypes(invalid)).rejects.toThrow("Invalid command payload reference");
  });
});
