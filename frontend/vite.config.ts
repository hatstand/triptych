import { defineConfig } from "vite";

// Each src/<name>.ts is a page script that the Jinja templates load as /static/js/<name>.js, so
// entries keep fixed names (no manifest to read back) and only shared chunks are hashed.
const ENTRIES = ["drop", "live", "passkey"];

export default defineConfig({
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
