<template>
  <div class="chat-page">
    <div class="chat-panel">
      <!-- 顶部说明 -->
      <div class="chat-header-hint">
        <el-icon><MagicStick /></el-icon>
        AI 助手会自动识别您的需求，路由到最合适的 Agent 为您服务
      </div>

      <!-- 消息区 -->
      <div class="chat-messages" ref="messagesEl">
        <div v-if="messages.length === 0 && !isStreaming" class="empty-hint">
          <p>✨ 您好！我是 FieldOps AI 助手</p>
          <p>请直接告诉我您的需求，例如：</p>
          <div class="example-queries">
            <el-tag
              v-for="q in exampleQueries"
              :key="q"
              class="example-tag"
              @click="sendExample(q)"
            >{{ q }}</el-tag>
          </div>
        </div>

        <template v-for="(msg, i) in messages" :key="i">
          <ChatBubble :role="msg.role" :sources="msg.sources">
            <!-- 澄清消息：带候选 Agent 按钮 -->
            <template v-if="msg.clarify">
              <p style="margin: 0 0 10px">{{ msg.clarify.message }}</p>
              <div class="clarify-candidates">
                <el-button
                  v-for="c in msg.clarify.candidates"
                  :key="c"
                  size="small"
                  type="primary"
                  plain
                  @click="sendWithAgent(msg.clarify.query, c)"
                >
                  {{ agentDisplay(c) }}
                </el-button>
              </div>
            </template>
            <template v-else>
              <MarkdownRenderer :content="msg.content" />
              <div v-if="msg.agentType" class="agent-tag">
                <el-tag size="small" type="info" effect="plain">
                  {{ agentDisplay(msg.agentType) }}
                </el-tag>
              </div>
            </template>
          </ChatBubble>
        </template>

        <!-- 流式气泡 -->
        <ChatBubble v-if="isStreaming" role="assistant">
          <span class="thinking">
            <span class="dot" /><span class="dot" /><span class="dot" />
            正在思考
          </span>
        </ChatBubble>
      </div>

      <!-- 输入区 -->
      <div class="chat-input-area">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="3"
          placeholder="直接描述您的需求，Enter 发送，Shift+Enter 换行"
          resize="none"
          :disabled="isStreaming"
          @keydown="handleKeydown"
        />
        <div class="input-actions">
          <el-button
            type="primary"
            :loading="isStreaming"
            :disabled="!inputText.trim() || isStreaming"
            @click="sendMessage()"
          >
            发送
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { MagicStick } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import ChatBubble from '@/components/chat/ChatBubble.vue'
import MarkdownRenderer from '@/components/chat/MarkdownRenderer.vue'
import { useAuthStore } from '@/stores/auth'
import { streamSSE, UnauthorizedError } from '@/api/sse'

const router = useRouter()
const auth = useAuthStore()

interface ClarifyInfo {
  message: string
  candidates: string[]
  query: string
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: string[]
  agentType?: string
  clarify?: ClarifyInfo
}

const messages = ref<Message[]>([])
const inputText = ref('')
const messagesEl = ref<HTMLElement>()
// 固定 session：同一用户始终复用同一 thread，避免 MemorySaver 无限累积
const sessionId = ref(`unified_${auth.user?.userId ?? 'guest'}`)

const isStreaming = ref(false)

const exampleQueries = [
  '老人跌倒后应该如何处置？',
  '帮我评估这位老人的照护等级',
  '鼻饲管的护理规范是什么？',
]

const AGENT_DISPLAY: Record<string, string> = {
  sop_qa: 'SOP 问答',
  alert: '告警研判',
  assessment: '老人评估',
  work_flow: '照护流程',
  auto: '自动路由',
}

function agentDisplay(t: string) {
  return AGENT_DISPLAY[t] ?? t
}

function sendExample(q: string) {
  inputText.value = q
  sendMessage()
}

function handleKeydown(e: KeyboardEvent) {
  if (e.isComposing) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

function sendWithAgent(query: string, agentType: string) {
  sendMessage(query, agentType)
}

async function sendMessage(overrideText?: string, agentType?: string) {
  const text = (overrideText ?? inputText.value).trim()
  if (!text || isStreaming.value) return

  if (!overrideText) inputText.value = ''
  messages.value.push({ role: 'user', content: text })
  await scrollToBottom()

  isStreaming.value = true

  let answerContent = ''
  let answerSources: string[] = []
  let answerAgent = ''

  try {
    await streamSSE('/chat', {
      session_id: sessionId.value,
      query: text,
      ...(agentType ? { agent_type: agentType } : {}),
    }, (event, data) => {
      if (event === 'answer') {
        answerContent = data.content ?? ''
        answerSources = normalizeSources(data.structured_output?.sources)
        answerAgent = data.agent_type ?? ''
      } else if (event === 'clarify') {
        messages.value.push({
          role: 'assistant',
          content: '',
          clarify: {
            message: data.message ?? '请选择您需要的服务',
            candidates: data.candidates ?? [],
            query: data.query ?? text,
          },
        })
      } else if (event === 'error') {
        throw new Error(data.error ?? 'SSE error')
      }
    }, auth.token)

    if (answerContent) {
      messages.value.push({
        role: 'assistant',
        content: answerContent,
        sources: answerSources,
        agentType: answerAgent,
      })
    }
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      auth.logout()
      router.push('/login')
      return
    }
    ElMessage.error('请求失败，请重试')
    console.error('[UnifiedChat SSE]', err)
  } finally {
    isStreaming.value = false
    await scrollToBottom()
  }
}

function normalizeSources(sources: unknown): string[] {
  if (!Array.isArray(sources)) return []
  return sources.map((s: unknown) => {
    if (typeof s === 'string') return s
    if (s && typeof s === 'object') {
      const o = s as Record<string, string>
      return [o.title, o.url].filter(Boolean).join(' ')
    }
    return String(s)
  })
}

async function scrollToBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}
</script>

<style scoped>
.chat-page {
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
.chat-header-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 20px;
  font-size: 13px;
  color: #8c8c8c;
  border-bottom: 1px solid #f0f0f0;
  background: #fafafa;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
.empty-hint {
  text-align: center;
  color: #8c8c8c;
  margin-top: 60px;
  font-size: 15px;
  line-height: 2;
}
.example-queries {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 12px;
}
.example-tag {
  cursor: pointer;
  transition: opacity 0.15s;
}
.example-tag:hover { opacity: 0.75; }
.clarify-candidates {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.agent-tag {
  margin-top: 8px;
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
</style>
