import { defineConfig } from "vite";

export default defineConfig({
  root: "web",
  publicDir: "../assets",
  build: {
    outDir: "../dist-web",
    emptyOutDir: true,
  },
  clearScreen: false,
  server: {
    strictPort: true,
  },
});
