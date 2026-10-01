import { defineConfig } from 'vite';

export default defineConfig({
  server: { host: true, proxy: { '/v1': 'http://localhost:8787' } },
  build: { outDir: 'dist', sourcemap: true, chunkSizeWarningLimit: 900 },
});
