#!/usr/bin/env node
// Offline compiler; deliberately has no cloud/GitHub clients or shell calls.
import { readFile, mkdir, writeFile, access } from "node:fs/promises";
import { resolve, dirname } from "node:path";
import { compileRequest } from "../site/model.mjs";
const [input, output] = process.argv.slice(2);
if (!input || !output) {
  console.error(
    "Usage: node scripts/configure.mjs request.json environments/NAME",
  );
  process.exit(2);
}
const files = compileRequest(JSON.parse(await readFile(input, "utf8"))),
  target = resolve(output);
let exists = false;
try {
  await access(target);
  exists = true;
} catch {}
if (exists)
  throw new Error(
    "Output already exists; review existing configuration instead of overwriting it",
  );
await mkdir(dirname(target), { recursive: true });
await mkdir(target);
for (const [name, data] of Object.entries(files))
  await writeFile(resolve(target, name), data, { flag: "wx" });
console.log(
  `Generated ${target}. Review README.md; no deployment or publication was performed.`,
);
