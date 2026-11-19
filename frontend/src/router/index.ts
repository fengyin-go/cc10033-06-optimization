import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ChangeExportView from '@/views/ChangeExportView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/slow_change_log/export-workbench', name: 'change-export', component: ChangeExportView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
