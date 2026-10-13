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
        // 作业规范 问答
        {
          path: 'sop-qa',
          name: 'sop-qa',
          component: () => import('@/views/sop_qa/SOPQAChatView.vue'),
        },
        // 作业流程
        {
          path: 'work-flow',
          name: 'work-flow',
          component: () => import('@/views/work_flow/WorkFlowSetupView.vue'),
        },
        {
          path: 'work-flow/:sessionId',
          name: 'work-flow-chat',
          component: () => import('@/views/work_flow/WorkFlowChatView.vue'),
        },
        // 服务计划：风险等级 / 服务项 / 当天执行进度（只读 + 执行回写）
        {
          path: 'work-flow/:sessionId/plan',
          name: 'service-plan',
          component: () => import('@/views/work_flow/ServicePlanView.vue'),
        },
        // 出站通知：SaaS 回调配置与事件审计（需要 supervisor/admin 角色）
        {
          path: 'outbound-webhook',
          name: 'outbound-webhook',
          component: () => import('@/views/work_flow/OutboundWebhookView.vue'),
          meta: { requiresSupervisor: true },
        },
        // 点位评估
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
