import { readFileSync } from "node:fs";
import { defineConfig, type Plugin } from "vite";
import icons from "./src/icons.json" with { type: "json" };

// Each src/<name>.ts is a page script that the Jinja templates load as /static/js/<name>.js, so
// entries keep fixed names (no manifest to read back) and only shared chunks are hashed.
const ENTRIES = ["drop", "live", "passkey"];

// Triptych's icon names -> Lucide icons (lucide.dev, ISC), emitted as one <symbol> sprite that the
// Jinja icon() macro in templates/_icons.html points <use> at. Lucide's own attributes (24px grid,
// 2px round stroke in currentColor) go on each symbol so a bare <use> still draws correctly.
function iconSprite(): Plugin {
  return {
    name: "triptych-icon-sprite",
    generateBundle() {
      const symbols = Object.entries(icons).map(([name, lucide]) => {
        const svg = readFileSync(new URL(`node_modules/lucide-static/icons/${lucide}.svg`, import.meta.url), "utf8");
        const body = svg.slice(svg.indexOf(">", svg.indexOf("<svg")) + 1, svg.lastIndexOf("</svg>")).trim();
        return `<symbol id="${name}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</symbol>`;
      });
      this.emitFile({
        type: "asset",
        fileName: "icons.svg",
        source: `<svg xmlns="http://www.w3.org/2000/svg"><!-- Lucide icons, ISC licence: https://lucide.dev/license -->\n${symbols.join("\n")}\n</svg>\n`,
      });
    },
  };
}

export default defineConfig({
  plugins: [iconSprite()],
  base: "/static/js/",
  build: {
    outDir: "../src/triptych/web/static/js", // gitignored; FastAPI serves it from /static
    emptyOutDir: true,
    target: "es2022",
    sourcemap: true,
    rollupOptions: {
      input: Object.fromEntries(ENTRIES.map((name) => [name, `src/${name}.ts`])),
      // htmx keeps an eval() for hx-on/js: attributes; live.ts turns that off (allowEval).
      onwarn(warning, warn) {
        if (warning.code === "EVAL" && warning.id?.includes("/htmx.org/")) return;
        warn(warning);
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
      },
    },
  },
});
