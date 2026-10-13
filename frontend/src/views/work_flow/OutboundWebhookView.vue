<template>
  <div class="outbound-view">
    <!-- 回调配置 -->
    <el-card shadow="never" class="config-card">
      <template #header>
        <div class="card-head">
          <span class="card-title">出站回调配置</span>
          <el-tag v-if="!configured" size="small" type="warning">未配置</el-tag>
          <el-tag v-else size="small" :type="current.enabled ? 'success' : 'info'">
            {{ current.enabled ? '已启用' : '已停用' }}
          </el-tag>
        </div>
      </template>

      <el-alert type="info" :closable="false" show-icon class="hint">
        <template #title>
          作业流程走到终态、告警派单/取消时，系统会把事件 POST 到该地址。
          投递走发件箱 + 指数退避重试（30s → 120s → 600s → 1800s），
          请求头 <code>X-FieldOps-Signature</code> 为 HMAC-SHA256 签名，
          <code>X-FieldOps-Event-Id</code> 供消费端幂等去重。
        </template>
      </el-alert>

      <el-form :model="form" label-width="110px" class="config-form">
        <el-form-item label="回调地址">
          <el-input
            v-model="form.url"
            placeholder="https://saas.example.com/hooks/fieldops-agent"
            clearable
          />
        </el-form-item>

        <el-form-item label="签名密钥">
          <el-input
            v-model="form.secret"
            :placeholder="current.secret ? `已配置（${current.secret}），留空表示不修改` : '留空则不签名'"
            show-password
            clearable
          />
        </el-form-item>

        <el-form-item label="启用投递">
          <el-switch v-model="form.enabled" />
          <span class="tip">关闭后事件只入发件箱、不投递（配置好后自动补投）</span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" @click="onSave">保存配置</el-button>
          <el-button :icon="Refresh" @click="loadAll">刷新</el-button>
        </el-form-item>
      </el-form>

      <div v-if="current.updated_at" class="meta">
        最近更新：{{ formatTime(current.updated_at) }}
      </div>
    </el-card>

    <!-- 事件发件箱 -->
    <el-card shadow="never" class="events-card">
      <template #header>
        <div class="card-head">
          <span class="card-title">出站事件（发件箱）</span>
          <div class="head-right">
            <span class="meta">共 {{ events.length }} 条</span>
            <el-button text :icon="Refresh" @click="loadEvents">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="loadingEvents"
        :data="events"
        size="small"
        empty-text="暂无出站事件"
      >
        <el-table-column prop="event_type" label="事件类型" width="170" />
        <el-table-column prop="aggregate_id" label="主体 ID" width="140" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="attempts" label="投递次数" width="90" align="center" />
        <el-table-column prop="last_error" label="最近失败原因" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ 'err-text': !!row.last_error }">{{ row.last_error || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button
              text
              type="primary"
              size="small"
              :disabled="row.status === 'pending'"
              @click="onRedeliver(row)"
            >
              重投
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="payload-tip">
        提示：点「重投」会把事件重置为等待投递状态；内容载荷（报告 / 工单）随事件存库，可通过
        <code>GET /api/v1/work-flow/events</code> 查看。
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { outboundApi, type OutboundEvent, type OutboundWebhookConfig } from '@/api/workFlow'

const configured = ref(false)
const current = reactive<OutboundWebhookConfig>({
  url: '', secret: '', enabled: true, updated_at: null,
})
const form = reactive({ url: '', secret: '', enabled: true })
const events = ref<OutboundEvent[]>([])
const saving = ref(false)
const loadingEvents = ref(false)

function formatTime(value?: string | null) {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

function statusLabel(status: string) {
  return { pending: '待投递', sent: '已送达', failed: '已失败' }[status] ?? status
}

function statusType(status: string) {
  if (status === 'sent') return 'success'
  if (status === 'failed') return 'danger'
  return 'warning'
}

async function loadWebhook() {
  const { data } = await outboundApi.getWebhook()
  configured.value = data.configured
  if (data.webhook) {
    Object.assign(current, data.webhook)
    form.url = data.webhook.url
    form.enabled = data.webhook.enabled
    form.secret = ''   // 密钥不回显，留空 = 保持原值
  }
}

async function loadEvents() {
  loadingEvents.value = true
  try {
    const { data } = await outboundApi.listEvents()
    events.value = data.events
  } finally {
    loadingEvents.value = false
  }
}

async function loadAll() {
  await Promise.all([loadWebhook(), loadEvents()])
}

async function onSave() {
  if (!form.url.trim()) {
    ElMessage.warning('请填写回调地址')
    return
  }
  saving.value = true
  try {
    // secret 为空串时不传该字段：后端语义为"不改动现有密钥"
    const payload: { url: string; secret?: string; enabled: boolean } = {
      url: form.url.trim(),
      enabled: form.enabled,
    }
    if (form.secret) payload.secret = form.secret

    await outboundApi.saveWebhook(payload)
    ElMessage.success('回调配置已保存')
    await loadWebhook()
  } finally {
    saving.value = false
  }
}

async function onRedeliver(row: OutboundEvent) {
  await ElMessageBox.confirm(
    `确认重投事件 ${row.event_id}（${row.event_type}）？`,
    '重投确认',
    { type: 'warning' },
  )
  await outboundApi.redeliver(row.event_id)
  ElMessage.success('已重置为待投递，投递器下一轮会重发')
  await loadEvents()
}

onMounted(loadAll)
</script>

<style scoped>
.outbound-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1180px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #232323;
}
.head-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.hint {
  margin-bottom: 16px;
}
.hint code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 12px;
}
.config-form {
  max-width: 640px;
}
.tip {
  margin-left: 12px;
  font-size: 12px;
  color: #8c8c8c;
}
.meta {
  font-size: 12px;
  color: #8c8c8c;
}
.err-text {
  color: #d4380d;
}
.payload-tip {
  margin-top: 12px;
  font-size: 12px;
  color: #8c8c8c;
}
.payload-tip code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 3px;
}
</style>
