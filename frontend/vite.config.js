import { defineConfig } from "vite";

export default defineConfig({
  // The view is served on its own path by the FastAPI app.
  base: "/v2/",
  build: {
    // The FastAPI app serves the built view; keep the artifact in its static tree.
    outDir: "../src/wikigraph/static/v2",
    emptyOutDir: true,
  },
});
