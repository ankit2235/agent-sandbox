// Copies the shared JSON Schemas (one copy, in the Python package) into dist/schemas for the
// build. Nothing is committed twice: dist/ is ignored.
import { cpSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "..", "src", "guard", "schemas");
const target = join(here, "..", "dist", "schemas");
mkdirSync(target, { recursive: true });
for (const name of readdirSync(source)) {
  if (name.endsWith(".schema.json")) cpSync(join(source, name), join(target, name));
}
