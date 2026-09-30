import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
});
