<template>
  <div class="alert-page">
    <!-- 第一步：触发告警 -->
    <el-card class="trigger-card">
      <template #header>
        <span>触发告警研判</span>
      </template>
      <el-form :inline="true" :model="form">
        <el-form-item label="点位 ID">
          <el-input v-model="form.site_id" placeholder="site_001" style="width: 160px" />
        </el-form-item>
        <el-form-item label="设备 ID">
          <el-input v-model="form.device_id" placeholder="device_001" style="width: 160px" />
        </el-form-item>
        <el-form-item label="告警类型">
          <el-select v-model="form.alert_type" style="width: 180px">
            <el-option v-for="t in alertTypes" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="triggering"
            :disabled="!form.site_id || !form.device_id"
            @click="handleTrigger"
          >
            {{ triggering ? 'AI 研判中…' : '触发研判' }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 第二步：审核详情 -->
    <el-card v-if="review" class="review-card">
      <template #header>
        <div class="review-header">
          <span>告警审核：{{ review.alert_id }}</span>
          <div class="header-tags">
            <el-tag size="small">{{ alertTypeLabel(review.alert_type) }}</el-tag>
            <el-tag size="small" :type="riskTagType">{{ riskLabel }}</el-tag>
            <el-tag size="small" type="info" effect="plain">{{ review.status }}</el-tag>
          </div>
        </div>
      </template>

      <!-- 风险评估 -->
      <div class="section-title">AI 风险评估</div>
      <div class="risk-section">
        <div v-for="[k, v] in riskEntries" :key="k" class="risk-item">
          <span class="risk-key">{{ k }}：</span>
          <span>{{ v }}</span>
        </div>
        <div v-if="!riskEntries.length" class="risk-item">（无结构化风险评估数据）</div>
      </div>

      <!-- 三轨数据 -->
      <el-collapse style="margin-top: 12px">
        <el-collapse-item title="设备实时数据（device_readings）" name="device">
          <pre class="track-json">{{ pretty(review.device_readings) }}</pre>
        </el-collapse-item>
        <el-collapse-item title="服务记录（service_records）" name="service">
          <pre class="track-json">{{ pretty(review.service_records) }}</pre>
        </el-collapse-item>
        <el-collapse-item title="健康指标趋势（health_indicators）" name="health">
          <pre class="track-json">{{ pretty(review.health_indicators) }}</pre>
        </el-collapse-item>
      </el-collapse>

      <!-- 确认操作 -->
      <el-divider />
      <div class="section-title">值班主管确认</div>
      <el-radio-group v-model="confirmAction" :disabled="confirming">
        <el-radio value="approve">确认派发</el-radio>
        <el-radio value="modify">修改后派发</el-radio>
        <el-radio value="dismiss">驳回误报</el-radio>
      </el-radio-group>
      <el-input
        v-model="comment"
        type="textarea"
        :rows="2"
        placeholder="审核备注（可选）"
        style="margin-top: 12px"
      />
      <el-button
        type="primary"
        style="margin-top: 12px"
        :loading="confirming"
        :disabled="!confirmAction"
        @click="handleConfirm"
      >
        提交确认
      </el-button>
    </el-card>

    <!-- 第三步：派发结果 -->
    <el-card v-if="result" class="result-card">
      <template #header>
        <span>处置结果</span>
      </template>
      <el-result
        :icon="result.status === 'dismissed' ? 'info' : 'success'"
        :title="result.status === 'dismissed' ? '告警已驳回' : '工单已派发'"
      />
      <pre class="track-json">{{ pretty(result.dispatch_order ?? result.structured_output) }}</pre>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { alertApi, type AlertReview, type AlertConfirmResponse } from '@/api/alertApi'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const form = reactive({
  site_id: '',
  device_id: '',
  alert_type: 'heart_rate_abnormal',
})

const alertTypes = [
  { value: 'overcurrent',          label: '配电过载' },
  { value: 'device_offline',       label: '设备离线' },
  { value: 'fall_detected',        label: '人员跌倒' },
  { value: 'intrusion_detected',   label: '人员闯入' },
  { value: 'air_quality_abnormal', label: '气体异常' },
  { value: 'fall_detected',       label: '跌倒检测' },
  { value: 'room_temp_abnormal',  label: '室温异常' },
]

function alertTypeLabel(t: string) {
  return alertTypes.find(x => x.value === t)?.label ?? t
}

const triggering = ref(false)
const review = ref<AlertReview | null>(null)
const confirmAction = ref<'approve' | 'modify' | 'dismiss'>('approve')
const comment = ref('')
const confirming = ref(false)
const result = ref<AlertConfirmResponse | null>(null)

const riskLabel = computed(() => review.value?.risk_level ?? '未知')
const riskTagType = computed(() => {
  const lv = (review.value?.risk_level ?? '').toLowerCase()
  if (lv.includes('high') || lv.includes('高')) return 'danger'
  if (lv.includes('medium') || lv.includes('中')) return 'warning'
  return 'success'
})

const riskEntries = computed(() => {
  const ra = review.value?.risk_assessment
  if (!ra || typeof ra !== 'object') return []
  return Object.entries(ra)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)] as [string, string])
})

function pretty(data: unknown) {
  return JSON.stringify(data ?? [], null, 2)
}

async function handleTrigger() {
  triggering.value = true
  review.value = null
  result.value = null
  try {
    const { data } = await alertApi.trigger({ ...form })
    ElMessage.success('AI 研判完成，等待审核')
    // 触发后从 DB 读取审核数据（告警链路已落库）
    const res = await alertApi.getReview(data.alert_id)
    review.value = res.data
  } catch (e) {
    console.error('[Alert trigger]', e)
    ElMessage.error('触发失败')
  } finally {
    triggering.value = false
  }
}

async function handleConfirm() {
  if (!review.value || !confirmAction.value) return
  confirming.value = true
  try {
    const { data } = await alertApi.confirm(review.value.alert_id, {
      action: confirmAction.value,
      modified_actions: [],
      supervisor_id: auth.user?.userId ?? '',
      comment: comment.value,
    })
    result.value = data
    ElMessage.success('处置完成')
    review.value = null
  } catch (e) {
    console.error('[Alert confirm]', e)
    ElMessage.error('确认失败')
  } finally {
    confirming.value = false
  }
}
</script>

<style scoped>
.alert-page {
  max-width: 860px;
}
.trigger-card,
.review-card,
.result-card {
  border-radius: 8px;
  margin-bottom: 16px;
}
.review-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-tags {
  display: flex;
  gap: 8px;
}
.section-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.risk-section {
  background: #fafafa;
  border-radius: 6px;
  padding: 12px 16px;
  font-size: 14px;
  line-height: 1.9;
}
.risk-key {
  color: #8c8c8c;
}
.track-json {
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  max-height: 300px;
  overflow: auto;
  white-space: pre-wrap;
}
</style>
