<template>
  <div class="setup-page">
    <el-card class="setup-card">
      <template #header>
        <div class="card-header">
          <span>发起照护流程</span>
        </div>
      </template>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        style="max-width: 480px"
      >
        <el-form-item label="老人 ID" prop="site_id">
          <el-input
            v-model="form.site_id"
            placeholder="例如：site_001"
            :prefix-icon="User"
          />
        </el-form-item>
        <el-form-item label="照护计划 ID（可选）">
          <el-input
            v-model="form.service_plan_id"
            placeholder="如已有照护计划可填写，用于流程注入"
          />
        </el-form-item>
        <el-form-item label="起始阶段">
          <el-select v-model="form.initial_stage" style="width: 100%">
            <el-option
              v-for="s in stageOptions"
              :key="s.value"
              :label="s.label"
              :value="s.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="退住工单（模拟 SaaS）">
          <el-switch
            v-model="form.planned_discharge"
            active-text="入住时已知要出院"
            inactive-text="续住（默认）"
          />
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          :loading="loading"
          style="width: 100%"
          @click="handleStart"
        >
          开始流程
        </el-button>
      </el-form>

      <el-alert
        type="info"
        :closable="false"
        style="margin-top: 16px"
        title="照护流程包含四个阶段：入住评估 → 日常照护 → 阶段复评 → 出院评估"
        description="日常照护内含异常子模式；阶段复评后由 SaaS 退住工单驱动出院 or 续住循环。结束后自动生成照护报告并落库。"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { User } from '@element-plus/icons-vue'
import { workFlowApi, type WorkFlowActiveSession } from '@/api/workFlow'

const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({
  site_id: '',
  service_plan_id: '',
  initial_stage: 'admission_assessment',
  planned_discharge: false,
})

const stageOptions = [
  { value: 'admission_assessment', label: '入住评估' },
  { value: 'daily_care',           label: '日常照护' },
  { value: 'periodic_review',      label: '阶段复评' },
  { value: 'discharge_assessment', label: '出院评估' },
]

const rules: FormRules = {
  site_id: [{ required: true, message: '请输入老人 ID', trigger: 'blur' }],
}

function stageLabel(value: string): string {
  return stageOptions.find((s) => s.value === value)?.label ?? value
}

/** 发起前先查该老人是否已有进行中的流程，有则弹窗询问：继续原会话 or 重新开始。 */
async function tryResumeActiveSession(): Promise<string> {
  let resumeSessionId = ''
  let active: WorkFlowActiveSession | null = null
  try {
    const { data } = await workFlowApi.getActiveSession(form.site_id)
    active = data.active ? data.session : null
  } catch {
    return '' // 查询失败不阻断发起
  }
  if (!active) return ''

  try {
    await ElMessageBox.confirm(
      `该老人有一场进行中的照护流程：当前阶段「${stageLabel(active.current_stage)}」，已进行 ${active.total_turns} 轮。` +
        '继续将接续原会话进度；重新开始则另起新流程（原流程保留）。',
      '发现未完成的照护流程',
      { confirmButtonText: '继续原流程', cancelButtonText: '重新开始', type: 'warning' },
    )
    resumeSessionId = active.session_id
  } catch {
    // 用户选择重新开始，走正常发起
  }
  return resumeSessionId
}

async function handleStart() {
  await formRef.value?.validate()
  loading.value = true
  try {
    const resumeSessionId = await tryResumeActiveSession()
    if (resumeSessionId) {
      router.push(`/work-flow/${resumeSessionId}`)
      return
    }

    const { data } = await workFlowApi.start({
      site_id: form.site_id,
      service_plan_id: form.service_plan_id || undefined,
      initial_stage: form.initial_stage,
      discharge_order: form.planned_discharge
        ? { order_type: 'discharge', reason: '入住时已知计划出院' }
        : undefined,
    })
    // 首条 AI 消息经 sessionStorage 传给对话页（后端无会话历史查询接口）
    sessionStorage.setItem(
      `workflow_${data.session_id}`,
      JSON.stringify({ aiMessage: data.ai_message, currentStage: data.current_stage }),
    )
    router.push(`/work-flow/${data.session_id}`)
  } catch (e) {
    console.error('[WorkFlow start]', e)
    ElMessage.error('发起流程失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.setup-page {
  max-width: 640px;
}
.setup-card {
  border-radius: 8px;
}
.card-header {
  font-weight: 600;
}
</style>
