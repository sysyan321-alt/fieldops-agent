<template>
  <div class="sidebar">
    <div class="logo">
      <span>🏥 FieldOps</span>
    </div>
    <nav class="nav-list">
      <RouterLink to="/dashboard" class="nav-item" :class="{ 'nav-item--active': isActive('/dashboard') }">
        <el-icon><House /></el-icon>
        <span>首页</span>
      </RouterLink>

      <RouterLink to="/chat" class="nav-item" :class="{ 'nav-item--active': isActive('/chat') }">
        <el-icon><MagicStick /></el-icon>
        <span>AI 助手</span>
      </RouterLink>

      <RouterLink to="/sop-qa" class="nav-item" :class="{ 'nav-item--active': isActive('/sop-qa') }">
        <el-icon><ChatDotRound /></el-icon>
        <span>SOP 问答</span>
      </RouterLink>

      <RouterLink to="/work-flow" class="nav-item" :class="{ 'nav-item--active': isActive('/work-flow') }">
        <el-icon><FirstAidKit /></el-icon>
        <span>照护流程</span>
      </RouterLink>

      <RouterLink to="/assessment" class="nav-item" :class="{ 'nav-item--active': isActive('/assessment') }">
        <el-icon><Document /></el-icon>
        <span>老人评估</span>
      </RouterLink>

      <!-- 站长端菜单（仅 supervisor/admin 可见） -->
      <template v-if="auth.isSupervisor">
        <div class="nav-divider" />
        <RouterLink to="/alert-review" class="nav-item" :class="{ 'nav-item--active': isActive('/alert-review') }">
          <el-icon><Bell /></el-icon>
          <span>告警审核</span>
        </RouterLink>

        <RouterLink to="/outbound-webhook" class="nav-item" :class="{ 'nav-item--active': isActive('/outbound-webhook') }">
          <el-icon><Connection /></el-icon>
          <span>出站通知</span>
        </RouterLink>
      </template>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import {
  House, ChatDotRound, Document, MagicStick,
  FirstAidKit, Bell, Connection,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()

// 仅用于子路由高亮（纯视觉反馈，不参与导航逻辑）
function isActive(prefix: string) {
  return route.path === prefix || route.path.startsWith(prefix + '/')
}
</script>

<style scoped>
.sidebar {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.logo {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 16px;
  font-weight: 600;
  border-bottom: 1px solid #ffffff1a;
  flex-shrink: 0;
}

.nav-list {
  display: flex;
  flex-direction: column;
  padding: 4px 0;
  flex: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 20px;
  color: #ffffffa6;
  text-decoration: none;
  font-size: 14px;
  cursor: pointer;
  transition: background-color 0.2s, color 0.2s;
  user-select: none;
}

.nav-item:hover {
  background-color: #ffffff14;
  color: #fff;
}

.nav-item--active {
  background-color: #1677ff;
  color: #fff;
}

.nav-item .el-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.nav-divider {
  border: none;
  border-top: 1px solid #ffffff1a;
  margin: 8px 0;
}
</style>
