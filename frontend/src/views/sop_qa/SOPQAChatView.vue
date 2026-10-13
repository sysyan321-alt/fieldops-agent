<template>
  <div class="qa-page">
    <!-- 左侧：会话列表 -->
    <div class="session-panel">
      <div class="session-header">
        <span>会话列表</span>
        <el-button :icon="Plus" circle size="small" @click="newSession" />
      </div>
      <div class="session-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="session-item"
          :class="{ active: s.id === currentSessionId }"
          @click="switchSession(s.id)"
        >
          <el-icon><ChatDotRound /></el-icon>
          <span class="session-name">{{ s.name }}</span>
          <el-icon class="delete-btn" @click.stop="deleteSession(s.id)"><Close /></el-icon>
        </div>
      </div>
    </div>

    <!-- 右侧：聊天区 -->
    <div class="chat-panel">
      <div class="chat-messages" ref="messagesEl">
        <div v-if="messages.length === 0 && !isStreaming" class="empty-hint">
          <p>🧑‍⚕️ 你好！我是作业规范 助手</p>
          <p>高处作业、配电巡检、消防检查、设备保养、受限空间、水浸处置等问题，随时问我</p>
        </div>

        <ChatBubble
          v-for="(msg, i) in messages"
          :key="i"
          :role="msg.role"
          :sources="msg.sources"
        >
          <MarkdownRenderer :content="msg.content" />
        </ChatBubble>

        <!-- 流式气泡 -->
        <ChatBubble v-if="isStreaming" role="assistant">
          <span class="thinking">
            <span class="dot" /><span class="dot" /><span class="dot" />
            正在检索作业规范知识库
          </span>
        </ChatBubble>
      </div>

      <div class="chat-input-area">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="3"
          placeholder="输入作业问题，Enter 发送，Shift+Enter 换行"
          resize="none"
          :disabled="isStreaming"
          @keydown="handleKeydown"
        />
        <div class="input-actions">
          <el-tag
            v-if="lastAnswerMode"
            size="small"
            :type="answerModeTagType"
          >
            {{ answerModeLabel }}
          </el-tag>
          <el-button
            type="primary"
            :loading="isStreaming"
            :disabled="!inputText.trim() || isStreaming"
            @click="sendMessage"
          >
            发送
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, ChatDotRound, Close } from '@element-plus/icons-vue'
import { v4 as uuidv4 } from 'uuid'
import { ElMessage } from 'element-plus'
import ChatBubble from '@/components/chat/ChatBubble.vue'
import MarkdownRenderer from '@/components/chat/MarkdownRenderer.vue'
import { useAuthStore } from '@/stores/auth'
import { streamSSE, UnauthorizedError } from '@/api/sse'

defineOptions({ name: 'SOPQAChatView' })

interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: string[]
}

interface Session {
  id: string
  name: string
  messages: Message[]
}

const router = useRouter()
const auth = useAuthStore()

const sessions = ref<Session[]>([])
const currentSessionId = ref('')
const messages = ref<Message[]>([])
const inputText = ref('')
const messagesEl = ref<HTMLElement>()

const isStreaming = ref(false)
const lastAnswerMode = ref('')

// ── 回答模式标签 ──────────────────────────────────────────────
const answerModeTagType = computed(() => {
  switch (lastAnswerMode.value) {
    case 'rag':           return 'success'   // 绿色：知识库命中
    case 'web_augmented': return 'primary'   // 蓝色：联网搜索增强
    case 'general':       return 'warning'   // 橙色：通用回答
    default:              return 'info'      // 灰色
  }
})

const answerModeLabel = computed(() => {
  switch (lastAnswerMode.value) {
    case 'rag':           return '知识库回答'
    case 'web_augmented': return '🌐 联网增强'
    case 'general':       return '通用回答'
    default:              return lastAnswerMode.value
  }
})

// ── 会话管理 ──────────────────────────────────────────────────

function newSession() {
  const id = `sopqa_session_${uuidv4()}`
  const session: Session = { id, name: `会话 ${sessions.value.length + 1}`, messages: [] }
  sessions.value.unshift(session)
  switchSession(id)
}

function switchSession(id: string) {
  if (isStreaming.value) return
  currentSessionId.value = id
  const s = sessions.value.find(s => s.id === id)
  // 直接引用 session.messages，后续只需操作 messages.value
  messages.value = s ? s.messages : []
  scrollToBottom()
}

function deleteSession(id: string) {
  const idx = sessions.value.findIndex(s => s.id === id)
  if (idx === -1) return
  sessions.value.splice(idx, 1)
  if (currentSessionId.value === id) {
    sessions.value.length > 0 ? switchSession(sessions.value[0].id) : newSession()
  }
}

// ── 键盘处理：Enter 发送，Shift+Enter 换行 ────────────────────

function handleKeydown(e: KeyboardEvent) {
  // IME 输入法合成期间（isComposing）不触发发送
  if (e.isComposing) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

// ── 发送消息（fetch 读 ReadableStream，消费命名事件 SSE）──────

async function sendMessage() {
  const text = inputText.value.trim()
  if (!text || isStreaming.value) return

  if (!currentSessionId.value) newSession()

  inputText.value = ''

  const session = sessions.value.find(s => s.id === currentSessionId.value)

  messages.value.push({ role: 'user', content: text })
  if (session && session.messages.filter(m => m.role === 'user').length === 1) {
    session.name = text.slice(0, 20) + (text.length > 20 ? '…' : '')
  }

  await scrollToBottom()

  isStreaming.value = true
  lastAnswerMode.value = ''

  let answerContent = ''
  let answerSources: string[] = []

  try {
    await streamSSE('/sop-qa/chat', {
      session_id: currentSessionId.value,
      query: text,
    }, (event, data) => {
      if (event === 'answer') {
        answerContent = data.content ?? ''
        answerSources = normalizeSources(data.sources)
        lastAnswerMode.value = data.answer_mode ?? ''
      } else if (event === 'error') {
        throw new Error(data.error ?? 'SSE error')
      }
    }, auth.token)

    if (answerContent) {
      messages.value.push({
        role: 'assistant',
        content: answerContent,
        sources: answerSources,
      })
    }
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      auth.logout()
      router.push('/login')
      return
    }
    ElMessage.error('请求失败，请重试')
    console.error('[SOP-QA SSE]', err)
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

onMounted(() => {
  newSession()
})
</script>

<style scoped>
.qa-page {
  display: flex;
  height: calc(100vh - 56px - 48px);
  gap: 16px;
}

/* ── 左侧会话列表 ── */
.session-panel {
  width: 200px;
  background: #fff;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.session-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid #f0f0f0;
  font-size: 14px;
  font-weight: 500;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #595959;
  transition: background 0.15s;
}
.session-item:hover { background: #f5f7fa; }
.session-item.active { background: #e6f4ff; color: #1677ff; }
.session-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.delete-btn {
  opacity: 0;
  flex-shrink: 0;
  color: #bfbfbf;
  transition: opacity 0.15s, color 0.15s;
}
.session-item:hover .delete-btn { opacity: 1; }
.delete-btn:hover { color: #ff4d4f !important; }

/* ── 右侧聊天区 ── */
.chat-panel {
  flex: 1;
  background: #fff;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
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

/* ── 思考中跳点动画 ── */
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
