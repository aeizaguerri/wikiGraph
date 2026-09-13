import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  build: {
    // FastAPI serves this committed bundle directly as the app's root view.
    outDir: "../src/wikigraph/static",
    // Keep the throwaway prototype outside the shipping build untouched.
    emptyOutDir: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
    // Keep modern unprefixed properties intact: lightningcss's default browser
    // targets dropped unprefixed `backdrop-filter` for its -webkit alias.
    cssTarget: "chrome100",
    cssMinify: "esbuild",
  },
});
