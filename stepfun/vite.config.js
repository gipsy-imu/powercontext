import { defineConfig } from 'vite';

// The optional model cache is served separately and stays out of the build.
export default defineConfig({ publicDir: false });
