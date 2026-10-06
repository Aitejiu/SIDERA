import { readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";
import ts from "typescript";

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function pointer(value: string): string {
  return value.replace(/~/g, "~0").replace(/\//g, "~1");
}

function commandRules(document: unknown): Map<string, boolean> {
  if (!record(document) || !record(document.components) || !record(document.components.schemas)
    || !record(document["x-sidera-command-payloads"])) throw new Error("Invalid command payload map");
  const schemas = document.components.schemas;
  const rules = new Map<string, boolean>();
  const prefix = "#/components/schemas/";
  for (const reference of Object.values(document["x-sidera-command-payloads"])) {
    if (!record(reference) || typeof reference.$ref !== "string" || !reference.$ref.startsWith(prefix)) {
      throw new Error("Invalid command payload reference");
    }
    const encoded = reference.$ref.slice(prefix.length);
    if (!encoded || encoded.includes("/") || /~(?![01])/.test(encoded)) throw new Error("Invalid command payload reference");
    const name = encoded.replace(/~1/g, "/").replace(/~0/g, "~");
    const schema = schemas[name];
    if (!record(schema) || !record(schema.properties)) throw new Error("Invalid command payload reference");
    const required: unknown = schema.required ?? [];
    if (!Array.isArray(required) || !required.every((key: unknown) => typeof key === "string")) {
      throw new Error("Invalid command payload required fields");
    }
    for (const key of Object.keys(schema.properties)) {
      rules.set(`${prefix}${pointer(name)}/${pointer(key)}`, required.includes(key));
    }
  }
  return rules;
}

export async function generateCpTypes(document: unknown): Promise<string> {
  const rules = commandRules(document);
  const nodes = await openapiTS(JSON.stringify(document), {
    defaultNonNullable: true,
    transformProperty(property, _schema, options) {
      if (options.path === undefined || !rules.has(options.path)) return;
      return ts.factory.updatePropertySignature(property, property.modifiers, property.name,
        rules.get(options.path) ? undefined : ts.factory.createToken(ts.SyntaxKind.QuestionToken), property.type);
    },
  });
  return COMMENT_HEADER + astToString(nodes);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const document: unknown = JSON.parse(await readFile(new URL("../openapi.json", import.meta.url), "utf8"));
  const generated = await generateCpTypes(document);
  await writeFile(new URL("../src/shared/api/schema.d.ts", import.meta.url), generated);
}
