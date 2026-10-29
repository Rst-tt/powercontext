import { defineConfig } from 'vite';

// The web app talks to the local API server; during development the API runs on
// 8787 and Vite proxies /api to it, so the page and the API share an origin.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
      // ONNX Runtime loads its wasm glue through a dynamic import; passing those
      // files straight through avoids Vite transforming them as modules.
      '/ort': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2022',
    outDir: 'dist',
  },
  optimizeDeps: {
    exclude: ['@huggingface/transformers'],
  },
});
