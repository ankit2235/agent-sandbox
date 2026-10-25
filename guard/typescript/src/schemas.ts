// The shared JSON Schemas (draft 2020-12), compiled once with Ajv2020. The build copies them
// from guard/src/guard/schemas into dist/schemas, next to this module's dist/src.
import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import type { ErrorObject, ValidateFunction } from "ajv/dist/2020.js";

const NAMES = ["decision", "backend-result", "policy", "request", "record"] as const;
export type SchemaName = (typeof NAMES)[number];

const ajv = new Ajv2020({ allErrors: false, strict: true, allowUnionTypes: true });
const validators = new Map<SchemaName, ValidateFunction>();
for (const name of NAMES) {
  const url = new URL(`../schemas/${name}.schema.json`, import.meta.url);
  ajv.addSchema(JSON.parse(readFileSync(url, "utf8")) as object);
}
for (const name of NAMES) {
  const found = ajv.getSchema(`https://github.com/prarabdha-soni/agent-sandbox/guard/${name}.schema.json`);
  if (found === undefined) throw new Error(`schema ${name} did not compile`);
  validators.set(name, found);
}

function describe(error: ErrorObject | undefined): string {
  if (error === undefined) return "invalid";
  return `${error.instancePath || "(top level)"}: ${error.message ?? "invalid"}`;
}

/** The first problem with ``value`` under a shared schema, or null when it is valid. */
export function problem(name: SchemaName, value: unknown): string | null {
  const validate = validators.get(name);
  if (validate === undefined) throw new Error(`no schema ${name}`);
  return validate(value) ? null : describe(validate.errors?.[0]);
}

const BASE = "https://github.com/prarabdha-soni/agent-sandbox/guard";

/** A delegation: per-tool rules in the policy's ``actions`` format (as policy.py's). */
const delegation = ajv.compile({
  type: "object",
  additionalProperties: { $ref: `${BASE}/policy.schema.json#/$defs/rules` },
});
const fields = ajv.compile({ $ref: `${BASE}/request.schema.json#/$defs/fields` });

/** The first problem with a delegation or a fields object, or null when it is valid. */
export function partProblem(part: "delegation" | "fields", value: unknown): string | null {
  const validate = part === "delegation" ? delegation : fields;
  return validate(value) ? null : describe(validate.errors?.[0]);
}
