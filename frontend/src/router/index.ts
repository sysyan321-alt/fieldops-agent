import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/components/layout/AppLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          redirect: '/dashboard',
        },
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
        },
        // AI 助手（统一入口，自动路由）
        {
          path: 'chat',
          name: 'chat',
          component: () => import('@/views/UnifiedChatView.vue'),
        },
        // 护理 SOP 问答
        {
          path: 'sop-qa',
          name: 'sop-qa',
          component: () => import('@/views/sop_qa/SOPQAChatView.vue'),
        },
        // 照护流程
        {
          path: 'care-flow',
          name: 'care-flow',
          component: () => import('@/views/care_flow/CareFlowSetupView.vue'),
        },
        {
          path: 'care-flow/:sessionId',
          name: 'care-flow-chat',
          component: () => import('@/views/care_flow/CareFlowChatView.vue'),
        },
        // 照护计划：护理等级 / 服务项 / 当天执行进度（只读 + 执行回写）
        {
          path: 'care-flow/:sessionId/plan',
          name: 'care-plan',
          component: () => import('@/views/care_flow/CarePlanView.vue'),
        },
        // 出站通知：SaaS 回调配置与事件审计（需要 supervisor/admin 角色）
        {
          path: 'outbound-webhook',
          name: 'outbound-webhook',
          component: () => import('@/views/care_flow/OutboundWebhookView.vue'),
          meta: { requiresSupervisor: true },
        },
        // 老人评估
        {
          path: 'assessment',
          name: 'assessment',
          component: () => import('@/views/assessment/AssessmentUploadView.vue'),
        },
        {
          path: 'assessment/:assessmentId',
          name: 'assessment-report',
          component: () => import('@/views/assessment/AssessmentReportView.vue'),
        },
        // 告警研判审核（需要 supervisor/admin 角色）
        {
          path: 'alert-review',
          name: 'alert-review',
          component: () => import('@/views/alert/AlertReviewView.vue'),
          meta: { requiresSupervisor: true },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/dashboard',
    },
  ],
})

router.beforeEach((to, _from, next) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    if (auth.isLoggedIn && to.name === 'login') return next('/dashboard')
    return next()
  }

  if (!auth.isLoggedIn) return next('/login')

  if (to.meta.requiresSupervisor && !auth.isSupervisor) return next('/dashboard')

  next()
})

export default router
