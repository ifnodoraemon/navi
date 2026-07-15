import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: '/ui/trace/',
  build: {
    outDir: '../src/navi/static/trace',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('react-syntax-highlighter')) return 'syntax'
          if (id.includes('react-markdown') || id.includes('/remark-') || id.includes('/rehype-')) {
            return 'markdown'
          }
          if (id.includes('lucide-react')) return 'icons'
          if (id.includes('axios') || id.includes('date-fns') || id.includes('react-json-view-lite')) {
            return 'data'
          }
          if (id.includes('/react/') || id.includes('/react-dom/')) return 'react'
        },
      },
    },
  },
})
