import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { persistPlugin } from '@/stores/persist'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import './styles/global.less'
import './styles/antd-override.less'
import App from './App.vue'
import router from './router'

const app = createApp(App)
const pinia = createPinia()
pinia.use(persistPlugin)
app.use(pinia)
app.use(router)
app.use(Antd)
app.mount('#app')
