<template>
  <div class="plan-page">
    <!-- 顶部：老人 + 护理等级 + 计划来源 -->
    <div class="plan-header">
      <div>
        <h2>照护计划</h2>
        <p class="sub">老人 {{ overview?.elder_id || elderIdFallback }} · 会话 {{ sessionId }}</p>
      </div>
      <div class="header-actions">
        <el-tag v-if="overview?.care_level" type="primary" size="large">
          护理等级：{{ overview.care_level }}
        </el-tag>
        <el-tag :type="sourceTag.type" size="large">{{ sourceTag.text }}</el-tag>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <!-- 计划缺失：给出下一步，而不是显示空表格 -->
    <el-alert
      v-if="overview?.plan_missing"
      type="warning"
      :closable="false"
      show-icon
      title="该老人当前没有照护计划"
      class="missing-alert"
    >
      <div class="missing-body">
        <p>{{ overview.message }}</p>
        <ol>
          <li>上传能力评估表，由评估结论生成照护计划；</li>
          <li>或在机构系统里为该老人登记照护服务项（项目 / 频次 / 护理时间段）。</li>
        </ol>
        <el-button type="primary" size="small" @click="router.push('/assessment')">
          去做老人评估
        </el-button>
      </div>
    </el-alert>

    <template v-else-if="overview">
      <!-- 服务项：业务配置决定做什么、多久一次 -->
      <el-card class="section" shadow="never">
        <template #header>
          <span class="section-title">照护服务项</span>
          <span class="section-hint">
            由机构业务配置决定，AI 不增删改（共 {{ overview.service_items.length }} 项）
          </span>
        </template>
        <el-table :data="overview.service_items" size="small" empty-text="没有登记照护服务项">
          <el-table-column prop="project_name" label="项目" min-width="140" />
          <el-table-column prop="frequency_name" label="频次" width="120" />
          <el-table-column label="护理时间窗" min-width="180">
            <template #default="{ row }">{{ formatSlots(row.nurse_time_slots) }}</template>
          </el-table-column>
          <el-table-column label="必选" width="80">
            <template #default="{ row }">
              <el-tag :type="row.is_required ? 'danger' : 'info'" size="small">
                {{ row.is_required ? '必选' : '选做' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 当天计划与执行进度 -->
      <el-card class="section" shadow="never">
        <template #header>
          <span class="section-title">今日执行</span>
          <span class="section-hint">
            已完成 {{ overview.progress.done }} / {{ overview.progress.total }}
            <template v-if="overview.progress.missed"> · 未完成 {{ overview.progress.missed }}</template>
          </span>
        </template>

        <el-progress
          :percentage="donePercent"
          :status="overview.progress.missed ? 'warning' : undefined"
          :stroke-width="14"
          class="progress-bar"
        />

        <el-table :data="overview.today_plan" size="small" empty-text="今天没有可执行的计划条目">
          <el-table-column prop="project_name" label="项目" min-width="130" />
          <el-table-column prop="nurse_time" label="计划时间" width="130" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="statusTag(row.status).type" size="small">
                {{ statusTag(row.status).text }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="执行" min-width="150">
            <template #default="{ row }">
              <span v-if="row.carer_name">
                {{ row.carer_name }}
                <span v-if="row.actual_care_time" class="muted">（{{ row.actual_care_time }}）</span>
              </span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button
                type="primary"
                link
                :loading="busyId === row.plan_item_id"
                @click="confirm(row, 'done')"
              >
                确认完成
              </el-button>
              <el-button
                type="danger"
                link
                :loading="busyId === row.plan_item_id"
                @click="confirm(row, 'missed')"
              >
                未完成
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <p class="footnote">
          漏做（未完成）会进入照护历史的异常记录，供后续对话与复评参考；
          计划条目由业务侧生成规则决定，这里只回写执行结果。
        </p>
      </el-card>

      <el-alert
        v-if="overview.special_notes"
        type="info"
        :closable="false"
        :title="`注意事项：${overview.special_notes}`"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { carePlanApi, type DailyPlanItem, type PlanOverview, type PlanSource } from '@/api/careFlow'

const route = useRoute()
const router = useRouter()

const sessionId = (route.params.sessionId as string) ?? ''
const elderIdFallback = ref('')
const overview = ref<PlanOverview | null>(null)
const loading = ref(false)
const busyId = ref('')

// 计划来源如实展示：让使用者知道"任务清单是谁定的"
const SOURCE_LABEL: Record<PlanSource, { text: string; type: 'success' | 'info' | 'warning' | 'danger' }> = {
  service_items: { text: '计划来源：机构服务项', type: 'success' },
  ai_assessment: { text: '计划来源：评估结论', type: 'info' },
  merged: { text: '计划来源：服务项 + 评估结论', type: 'success' },
  pipeline: { text: '计划来源：评估流水线', type: 'info' },
  missing: { text: '计划来源：缺失', type: 'danger' },
}

const sourceTag = computed(
  () => SOURCE_LABEL[overview.value?.plan_source ?? 'missing'] ?? SOURCE_LABEL.missing,
)

const donePercent = computed(() => {
  const p = overview.value?.progress
  if (!p || !p.total) return 0
  return Math.round((p.done / p.total) * 100)
})

function statusTag(status: DailyPlanItem['status']) {
  if (status === 'done') return { text: '已完成', type: 'success' as const }
  if (status === 'missed') return { text: '未完成', type: 'danger' as const }
  return { text: '待执行', type: 'info' as const }
}

function formatSlots(slots?: Array<{ startTime?: string; endTime?: string }>) {
  if (!slots || slots.length === 0) return '—'
  return slots.map(s => `${s.startTime ?? ''}-${s.endTime ?? ''}`).join('、')
}

async function load() {
  if (!sessionId) {
    ElMessage.error('缺少会话 ID')
    return
  }
  loading.value = true
  try {
    const { data } = await carePlanApi.getOverview(sessionId)
    overview.value = data
    elderIdFallback.value = data.elder_id
  } catch {
    // 拦截器已提示错误（404 = 会话不存在或跨租户）
  } finally {
    loading.value = false
  }
}

async function confirm(row: DailyPlanItem, status: 'done' | 'missed') {
  busyId.value = row.plan_item_id
  try {
    const now = new Date()
    const hhmm = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
    await carePlanApi.confirmItem(row.plan_item_id, {
      status,
      actual_care_time: status === 'done' ? hhmm : '',
    })
    ElMessage.success(status === 'done' ? '已记录完成' : '已记录未完成')
    await load()
  } finally {
    busyId.value = ''
  }
}

onMounted(load)
</script>

<style scoped>
.plan-page {
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.plan-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.plan-header h2 {
  margin: 0 0 4px;
}

.sub {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-title {
  font-weight: 600;
}

.section-hint {
  margin-left: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.progress-bar {
  margin: 4px 0 14px;
}

.missing-body ol {
  margin: 8px 0;
  padding-left: 20px;
}

.footnote {
  margin: 12px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.muted {
  color: var(--el-text-color-secondary);
}
</style>
