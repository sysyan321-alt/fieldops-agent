<template>
  <div class="cf-page">
    <div class="chat-panel">
      <!-- 顶部：阶段进度 -->
      <div class="stage-panel">
        <span class="session-id">会话 {{ sessionId }}</span>
        <StageProgressBar :current="currentStage" :mode="dailyCareMode" />
        <el-button
          size="small"
          text
          type="primary"
          class="plan-entry"
          @click="router.push(`/care-flow/${sessionId}/plan`)"
        >
          照护计划
        </el-button>
      </div>

      <!-- 消息区 -->
      <div class="chat-messages" ref="messagesEl">
        <div v-if="messages.length === 0 && !isStreaming" class="empty-hint">
          <p>📋 照护流程对话即将开始</p>
        </div>

        <ChatBubble
          v-for="(msg, i) in messages"
          :key="i"
          :role="msg.role"
        >
          <MarkdownRenderer :content="msg.content" />
        </ChatBubble>

        <ChatBubble v-if="isStreaming" role="assistant">
          <span class="thinking">
            <span class="dot" /><span class="dot" /><span class="dot" />
            正在分析
          </span>
        </ChatBubble>
      </div>

      <!-- 完成提示 -->
      <el-alert
        v-if="isFinished"
        type="success"
        :closable="false"
        class="finish-alert"
        title="照护流程已完成"
      >
        <div>共 {{ totalTurns }} 轮对话，报告已生成并保存。</div>
        <el-button type="primary" size="small" style="margin-top: 8px" @click="openReport">
          查看报告
        </el-button>
      </el-alert>

      <!-- 输入区 -->
      <div class="chat-input-area">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="3"
          placeholder="输入回复，Enter 发送，Shift+Enter 换行"
          resize="none"
          :disabled="isStreaming || isFinished"
          @keydown="handleKeydown"
        />
        <div class="input-actions">
          <el-button
            v-if="!isFinished"
            :type="pendingDischargeOrder ? 'warning' : 'default'"
            size="default"
            :disabled="isStreaming"
            @click="toggleDischargeOrder"
          >
            {{ pendingDischargeOrder ? '退住工单已就绪（点击取消）' : '模拟 SaaS 下发退住工单' }}
          </el-button>
          <el-button
            type="primary"
            :loading="isStreaming"
            :disabled="!inputText.trim() || isStreaming || isFinished"
            @click="sendMessage"
          >
            发送
          </el-button>
        </div>
      </div>
    </div>

    <!-- 报告弹窗 -->
    <el-dialog v-model="showReport" title="照护评估报告" width="640px">
      <div v-if="reportLoading" style="text-align: center; padding: 40px">
        <el-icon class="is-loading" :size="24"><Loading /></el-icon>
      </div>
      <template v-else-if="report">
        <el-descriptions :column="2" border size="small" style="margin-bottom: 16px">
          <el-descriptions-item label="老人 ID">{{ report.elder_id }}</el-descriptions-item>
          <el-descriptions-item label="会话 ID">{{ report.session_id }}</el-descriptions-item>
          <el-descriptions-item label="到达阶段">{{ stageLabel(report.stage_reached) }}</el-descriptions-item>
          <el-descriptions-item label="对话轮数">{{ report.total_turns }}</el-descriptions-item>
        </el-descriptions>
        <pre class="report-json">{{ JSON.stringify(report.report, null, 2) }}</pre>
      </template>
      <el-empty v-else description="报告加载失败" />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Loading } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import ChatBubble from '@/components/chat/ChatBubble.vue'
import MarkdownRenderer from '@/components/chat/MarkdownRenderer.vue'
import StageProgressBar from '@/components/care_flow/StageProgressBar.vue'
import { useAuthStore } from '@/stores/auth'
import { streamSSE, UnauthorizedError } from '@/api/sse'
import { careFlowApi, type CareFlowReport } from '@/api/careFlow'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

interface Message {
  role: 'user' | 'assistant'
  content: string
}

const sessionId = (route.params.sessionId as string) ?? ''
const messages = ref<Message[]>([])
const inputText = ref('')
const messagesEl = ref<HTMLElement>()

const isStreaming = ref(false)
const currentStage = ref('admission_assessment')
const dailyCareMode = ref('normal')
const totalTurns = ref(0)
const isFinished = ref(false)
const showReport = ref(false)
const report = ref<CareFlowReport | null>(null)
const reportLoading = ref(false)
const pendingDischargeOrder = ref(false)  // 标记：下一条消息将携带 SaaS 退住工单

const STAGE_LABELS: Record<string, string> = {
  admission_assessment: '入住评估',
  daily_care: '日常照护',
  periodic_review: '阶段复评',
  discharge_assessment: '出院评估',
  finished: '已完成',
}

function stageLabel(key?: string, mode?: string) {
  const label = STAGE_LABELS[key ?? ''] ?? key ?? '-'
  if (key === 'daily_care' && mode === 'abnormal') {
    return `${label}（异常处置）`
  }
  return label
}

function handleKeydown(e: KeyboardEvent) {
  if (e.isComposing) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

function toggleDischargeOrder() {
  pendingDischargeOrder.value = !pendingDischargeOrder.value
}

async function openReport() {
  showReport.value = true
  if (!report.value) {
    reportLoading.value = true
    try {
      const { data } = await careFlowApi.getReport(sessionId)
      report.value = data
    } catch {
      report.value = null
    } finally {
      reportLoading.value = false
    }
  }
}

async function sendMessage() {
  const text = inputText.value.trim()
  if (!text || isStreaming.value || isFinished.value) return

  inputText.value = ''
  messages.value.push({ role: 'user', content: text })
  await scrollToBottom()

  isStreaming.value = true

  try {
    const body: Record<string, unknown> = {
      session_id: sessionId,
      message: text,
    }
    if (pendingDischargeOrder.value) {
      body.discharge_order = { order_type: 'discharge', reason: 'SaaS 下发退住工单' }
      pendingDischargeOrder.value = false
    }
    await streamSSE('/care-flow/chat', body, (event, data) => {
      if (event === 'message') {
        if (data.content) {
          messages.value.push({ role: 'assistant', content: data.content })
        }
        currentStage.value = data.current_stage ?? currentStage.value
        dailyCareMode.value = data.daily_care_mode ?? dailyCareMode.value
        totalTurns.value = data.total_turn_count ?? totalTurns.value
      } else if (event === 'report') {
        isFinished.value = true
      } else if (event === 'error') {
        throw new Error(data.error ?? 'SSE error')
      }
    }, auth.token)
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      auth.logout()
      router.push('/login')
      return
    }
    ElMessage.error('请求失败，请重试')
    console.error('[CareFlow SSE]', err)
  } finally {
    isStreaming.value = false
    await scrollToBottom()
  }
}

async function scrollToBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

onMounted(() => {
  // 恢复发起页传来的首条 AI 消息与初始阶段
  const raw = sessionStorage.getItem(`careflow_${sessionId}`)
  if (raw) {
    sessionStorage.removeItem(`careflow_${sessionId}`)
    try {
      const parsed = JSON.parse(raw)
      if (parsed.aiMessage) messages.value.push({ role: 'assistant', content: parsed.aiMessage })
      if (parsed.currentStage) currentStage.value = parsed.currentStage
    } catch { /* ignore */ }
  }
})
</script>

<style scoped>
.cf-page {
  display: flex;
  height: calc(100vh - 56px - 48px);
}
.chat-panel {
  flex: 1;
  background: #fff;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.stage-panel {
  padding: 8px 20px;
  border-bottom: 1px solid #f0f0f0;
  background: #fafafa;
}
.session-id {
  font-size: 12px;
  color: #bfbfbf;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
.empty-hint {
  text-align: center;
  color: #8c8c8c;
  margin-top: 80px;
  font-size: 15px;
  line-height: 2;
}
.finish-alert {
  margin: 0 16px 8px;
}
.chat-input-area {
  border-top: 1px solid #f0f0f0;
  padding: 12px 16px;
}
.input-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.thinking {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 0;
  font-size: 13px;
  color: #8c8c8c;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #bfbfbf;
  animation: bounce 1.2s infinite ease-in-out;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.7); opacity: 0.4; }
  40%            { transform: scale(1.1); opacity: 1; }
}
.report-json {
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
}
</style>
