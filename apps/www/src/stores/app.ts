import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { DashboardSummary } from '@/api/client'
import { analysisApi } from '@/api'

export const useAppStore = defineStore(
  'app',
  () => {
    const collapsed = ref(false)
    const dashboardData = ref<DashboardSummary | null>(null)
    const loading = ref(false)

    function toggleCollapsed() {
      collapsed.value = !collapsed.value
    }

    async function fetchDashboard() {
      loading.value = true
      try {
        const { data } = await analysisApi.dashboard()
        dashboardData.value = data
      } finally {
        loading.value = false
      }
    }

    return { collapsed, dashboardData, loading, toggleCollapsed, fetchDashboard }
  },
  {
    persist: {
      pick: ['collapsed'],
    },
  },
)
