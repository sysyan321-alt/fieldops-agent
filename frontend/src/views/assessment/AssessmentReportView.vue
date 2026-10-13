<template>
  <div class="report-page" v-loading="loading">
    <!-- 顶部：综合得分 -->
    <el-card v-if="detail" class="score-card">
      <div class="score-row">
        <div class="score-block">
          <div class="score-number" :style="{ color: scoreColor }">
            {{ detail.weighted_score }}
          </div>
          <div class="score-label">加权综合得分</div>
        </div>
        <div class="score-summary">
          <template v-if="summaryEntries.length">
            <div v-for="[k, v] in summaryEntries" :key="k" class="summary-item">
              <span class="summary-key">{{ k }}：</span>
              <span>{{ v }}</span>
            </div>
          </template>
          <div v-else class="summary-item">评估 ID：{{ detail.assessment_id }}</div>
        </div>
      </div>
    </el-card>

    <!-- 五维度评分 -->
    <el-row v-if="detail" :gutter="16" class="dim-row">
      <el-col :span="8" v-for="d in detail.dimension_scores" :key="d.key">
        <DimensionScoreCard
          :dimension="d.dimension"
          :score="d.score"
          :weight="d.weight"
          :issues="d.issues ?? []"
          :suggestions="d.suggestions ?? []"
        />
      </el-col>
    </el-row>

    <!-- 问题清单 -->
    <el-card v-if="detail && detail.issues.length" class="issues-card">
      <template #header>
        <span>问题清单（按优先级排序）</span>
      </template>
      <div v-for="(issue, i) in detail.issues" :key="i" class="issue-item">
        <el-tag :type="priorityTag(issue.priority)" size="small">
          {{ priorityLabel(issue.priority) }}
        </el-tag>
        <el-tag size="small" type="info" effect="plain">{{ issue.dimension }}</el-tag>
        <span class="issue-desc">{{ issue.description }}</span>
        <div v-if="issue.suggestion" class="issue-suggestion">建议：{{ issue.suggestion }}</div>
      </div>
    </el-card>

    <el-empty v-if="!loading && !detail" description="评估记录不存在" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import DimensionScoreCard from '@/components/assessment/DimensionScoreCard.vue'
import { assessmentApi, type AssessmentResponse } from '@/api/assessment'

const route = useRoute()

const loading = ref(true)
const detail = ref<AssessmentResponse | null>(null)

const scoreColor = computed(() => {
  const s = detail.value?.weighted_score ?? 0
  if (s >= 80) return '#52c41a'
  if (s >= 60) return '#faad14'
  return '#ff4d4f'
})

// summary 为 dict：渲染为键值对
const summaryEntries = computed(() => {
  const s = detail.value?.summary
  if (!s || typeof s !== 'object') return []
  return Object.entries(s)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)] as [string, string])
})

function priorityTag(p: string) {
  if (p === 'high') return 'danger'
  if (p === 'medium') return 'warning'
  return 'info'
}

function priorityLabel(p: string) {
  if (p === 'high') return '高'
  if (p === 'medium') return '中'
  return '低'
}

onMounted(async () => {
  try {
    const { data } = await assessmentApi.get(route.params.assessmentId as string)
    detail.value = data
  } catch {
    detail.value = null
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.report-page {
  max-width: 1100px;
}
.score-card {
  border-radius: 8px;
  margin-bottom: 16px;
}
.score-row {
  display: flex;
  align-items: center;
  gap: 32px;
}
.score-block {
  text-align: center;
  flex-shrink: 0;
}
.score-number {
  font-size: 56px;
  font-weight: 700;
  line-height: 1.1;
}
.score-label {
  font-size: 13px;
  color: #8c8c8c;
  margin-top: 4px;
}
.score-summary {
  flex: 1;
  font-size: 14px;
  color: #595959;
  line-height: 1.9;
}
.dim-row {
  margin-bottom: 16px;
}
.issues-card {
  border-radius: 8px;
}
.issue-item {
  padding: 10px 0;
  border-bottom: 1px solid #f5f5f5;
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.issue-item:last-child {
  border-bottom: none;
}
.issue-desc {
  font-size: 14px;
  color: #333;
}
.issue-suggestion {
  width: 100%;
  font-size: 13px;
  color: #52c41a;
  padding-left: 4px;
}
</style>
