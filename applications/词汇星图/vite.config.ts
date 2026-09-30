import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const apiPort = (globalThis as { process?: { env?: Record<string, string> } }).process?.env?.VITE_API_PORT || '5186';

export default defineConfig({
  plugins: [react()],
  base: './',
  server: { strictPort: true, proxy: { '/api': `http://127.0.0.1:${apiPort}`, '/health': `http://127.0.0.1:${apiPort}` } },
  build: { outDir: '../../outputs/词汇星图/build', emptyOutDir: true },
});
