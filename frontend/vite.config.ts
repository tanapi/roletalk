import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import fs from 'node:fs'
import path from 'node:path'

const certDir = path.resolve(__dirname, 'certs')
const keyPath = path.join(certDir, 'dev.key')
const certPath = path.join(certDir, 'dev.crt')
const https =
  fs.existsSync(keyPath) && fs.existsSync(certPath)
    ? {
        key: fs.readFileSync(keyPath),
        cert: fs.readFileSync(certPath),
      }
    : undefined

export default defineConfig({
  plugins: [vue()],
  server: {
    https,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://backend:8000',
        changeOrigin: true,
      },
    },
  },
})
