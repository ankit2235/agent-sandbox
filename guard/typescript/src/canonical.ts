// Canonical JSON (RFC 8785) and the SHA-256 hashes made from it, as guard/canonical.py.
//
// Numbers are written by JSON.stringify, which is ECMAScript's Number::toString and so
// RFC 8785's number form (1e-7, 1e+21, 5 for 5.0, 0 for -0). Object keys are sorted by
// UTF-16 code units (Array.prototype.sort's default). NaN, infinity, a lone surrogate and a
// value that is not JSON have no canonical form and throw.
import { createHash } from "node:crypto";

export class CanonicalError extends Error {}

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

function string(value: string): string {
  if (LONE_SURROGATE.test(value)) {
    throw new CanonicalError("a string holds a lone surrogate, which has no canonical form");
  }
  return JSON.stringify(value);
}

export function canonical(value: unknown): string {
  if (value === null) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new CanonicalError(`${value} has no JSON form`);
    return JSON.stringify(value);
  }
  if (typeof value === "string") return string(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonical(item)).join(",")}]`;
  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record).sort();
    return `{${keys.map((key) => `${string(key)}:${canonical(record[key])}`).join(",")}}`;
  }
  throw new CanonicalError(`${typeof value} is not a JSON value`);
}

export function digest(value: unknown): string {
  return createHash("sha256").update(canonical(value), "utf8").digest("hex");
}
