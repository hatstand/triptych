// Copies the browser libraries we serve as-is (pinned in package.json) next to the compiled
// TypeScript: `node vendor.ts [outDir]`, defaulting to tsconfig's outDir.
import { copyFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const FILES = ["htmx.org/dist/htmx.min.js", "htmx-ext-sse/dist/sse.min.js"];

const out = join(process.argv[2] ?? "../src/triptych/web/static/js", "vendor");
mkdirSync(out, { recursive: true });
for (const f of FILES) copyFileSync(join("node_modules", f), join(out, f.split("/").at(-1) ?? f));
