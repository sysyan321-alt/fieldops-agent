<template>
  <div class="dashboard">
    <div class="welcome">
      <h2>欢迎回来，{{ auth.user?.username ?? auth.user?.userId }}</h2>
      <p>选择一个功能开始工作</p>
    </div>

    <!-- AI 助手入口（突出展示） -->
    <el-row :gutter="16" style="margin-bottom: 16px">
      <el-col :span="24">
        <el-card
          class="ai-assistant-card"
          shadow="hover"
          @click="router.push('/chat')"
        >
          <div class="ai-card-content">
            <div class="ai-card-left">
              <span class="ai-icon">✨</span>
              <div>
                <div class="ai-title">AI 助手</div>
                <div class="ai-desc">直接描述您的需求，AI 自动识别意图并路由到最合适的 Agent — 支持评估→照护流程多 Agent 协同</div>
              </div>
            </div>
            <el-button type="primary" size="default">立即体验 →</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 四个独立功能入口 -->
    <el-row :gutter="16" class="feature-cards">
      <el-col :span="6" v-for="card in featureCards" :key="card.route">
        <el-card
          class="feature-card"
          shadow="hover"
          @click="router.push(card.route)"
        >
          <div class="card-icon">{{ card.icon }}</div>
          <div class="card-title">{{ card.title }}</div>
          <div class="card-desc">{{ card.desc }}</div>
          <el-button type="primary" plain size="small" style="margin-top: 12px">
            {{ card.action }}
          </el-button>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const featureCards = [
  {
    icon: '🤖',
    title: 'SOP 问答',
    desc: '护理规范即问即答，RAG 知识库 + 联网搜索双保障',
    action: '开始问答',
    route: '/sop-qa',
  },
  {
    icon: '📋',
    title: '照护流程',
    desc: '建档评估→日常巡检→异常处置→复评→出院 五阶段引导',
    action: '发起流程',
    route: '/work-flow',
  },
  {
    icon: '🧓',
    title: '老人评估',
    desc: '上传评估表 PDF，五维度照护等级评定与问题清单',
    action: '上传评估表',
    route: '/assessment',
  },
  {
    icon: '🔔',
    title: '告警审核',
    desc: '设备告警 AI 风险研判，站长人工确认后派发工单',
    action: '处理告警',
    route: '/alert-review',
  },
]
</script>

<style scoped>
.dashboard {
  max-width: 1100px;
}
.welcome {
  margin-bottom: 24px;
}
.welcome h2 {
  margin: 0 0 4px;
  font-size: 22px;
}
.welcome p {
  margin: 0;
  color: #8c8c8c;
}
.ai-assistant-card {
  cursor: pointer;
  background: linear-gradient(135deg, #f0f7ff 0%, #e6f4ff 100%);
  border: 1px solid #bae0ff;
  transition: transform 0.2s;
}
.ai-assistant-card:hover { transform: translateY(-2px); }
.ai-card-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 0;
}
.ai-card-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.ai-icon { font-size: 36px; }
.ai-title {
  font-size: 17px;
  font-weight: 600;
  color: #1677ff;
  margin-bottom: 4px;
}
.ai-desc {
  font-size: 13px;
  color: #595959;
}
.feature-card {
  cursor: pointer;
  text-align: center;
  padding: 8px 0;
  transition: transform 0.2s;
}
.feature-card:hover {
  transform: translateY(-2px);
}
.card-icon {
  font-size: 40px;
  margin-bottom: 12px;
}
.card-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}
.card-desc {
  font-size: 13px;
  color: #8c8c8c;
  line-height: 1.5;
  min-height: 48px;
}
</style>
