import { readFileSync } from "node:fs";
import { Resvg } from "@resvg/resvg-js";
import { defineConfig, type Plugin } from "vite";
import icons from "./src/icons.json" with { type: "json" };

// Each src/<name>.ts is a page script and src/style.css the stylesheet. Every output file name
// carries a content hash, so a deploy never meets a stale copy in a browser or Cloudflare's cache;
// the Jinja static('<name>') helper (src/triptych/web/assets.py) finds the hashed name in Vite's
// manifest.
const ENTRIES = ["copy.ts", "dialog.ts", "drop.ts", "live.ts", "passkey.ts", "style.css"];

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
        name: "icons.svg",
        originalFileName: "src/icons.svg", // its manifest key, like the entries'
        source: `<svg xmlns="http://www.w3.org/2000/svg"><!-- Lucide icons, ISC licence: https://lucide.dev/license -->\n${symbols.join("\n")}\n</svg>\n`,
      });
    },
  };
}

// Triptych's own mark, src/brand/mark.svg, is the favicon and the top-bar logo as it is. The home-screen
// icon is the same mark on a white tile, rendered to PNG because iOS won't take an SVG
// apple-touch-icon (and draws its own rounded corners, so the tile is square).
function brandIcons(): Plugin {
  const mark = new URL("src/brand/mark.svg", import.meta.url);
  return {
    name: "triptych-brand-icons",
    buildStart() {
      this.addWatchFile(mark.pathname);
    },
    generateBundle() {
      const svg = readFileSync(mark, "utf8");
      this.emitFile({ type: "asset", name: "mark.svg", originalFileName: "src/brand/mark.svg", source: svg });
      const body = svg.slice(svg.indexOf(">", svg.indexOf("<svg")) + 1, svg.lastIndexOf("</svg>"));
      const tile = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" fill="#fff"/><g transform="translate(3.6 3.6) scale(0.7)">${body}</g></svg>`;
      this.emitFile({
        type: "asset",
        name: "apple-touch-icon.png",
        originalFileName: "src/brand/apple-touch-icon.png",
        source: new Resvg(tile, { fitTo: { mode: "width", value: 180 } }).render().asPng(),
      });
    },
  };
}

export default defineConfig({
  plugins: [iconSprite(), brandIcons()],
  base: "/static/",
  build: {
    outDir: "../src/triptych/web/static", // gitignored; FastAPI serves it from /static
    manifest: true, // .vite/manifest.json
    emptyOutDir: true,
    target: "es2022",
    sourcemap: true,
    rollupOptions: {
      input: ENTRIES.map((name) => `src/${name}`),
      // htmx keeps an eval() for hx-on/js: attributes; live.ts turns that off (allowEval).
      onwarn(warning, warn) {
        if (warning.code === "EVAL" && warning.id?.includes("/htmx.org/")) return;
        warn(warning);
      },
      // Everything under assets/ is hashed, so the web app serves it as immutable.
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
