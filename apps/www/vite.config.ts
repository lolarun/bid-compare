/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  css: {
    preprocessorOptions: {
      less: {
        javascriptEnabled: true,
      },
    },
  },
  server: {
    port: 5120,
    // 不写 host 时 Vite 默认只解析 "localhost" 这一个字符串，在这台机器上被
    // Node 解析成只监听 IPv6 回环（[::1]），IPv4 的 127.0.0.1/localhost 连不上——
    // 用户反馈"服务端是不是没有启动"实际是这个，不是进程没起来（netstat 能看到
    // 端口在监听，只是只监听了一个协议族）。host:true 让 Vite 同时监听 IPv4+IPv6。
    host: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8020',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
  },
})
