import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  build: {
    // FastAPI serves this committed bundle directly as the app's root view.
    outDir: "../src/wikigraph/static",
    // The static tree is exclusively generated output; clear retired views and
    // stale bundles on every build.
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
    // Keep modern unprefixed properties intact: lightningcss's default browser
    // targets dropped unprefixed `backdrop-filter` for its -webkit alias.
    cssTarget: "chrome100",
    cssMinify: "esbuild",
  },
});
