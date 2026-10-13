<template>
  <div class="upload-page">
    <el-card class="upload-card">
      <template #header>
        <div class="card-header">
          <span>上传老人评估表</span>
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
          <el-input v-model="form.site_id" placeholder="例如：site_001" :prefix-icon="User" />
        </el-form-item>
        <el-form-item label="评估表 PDF" prop="file">
          <el-upload
            drag
            :auto-upload="false"
            :limit="1"
            accept=".pdf"
            :on-change="handleFileChange"
            :on-remove="() => (file = null)"
          >
            <el-icon :size="40"><UploadFilled /></el-icon>
            <div>将 PDF 文件拖到此处，或点击选择</div>
          </el-upload>
        </el-form-item>
        <el-button
          type="primary"
          size="large"
          :loading="loading"
          :disabled="!form.site_id || !file"
          style="width: 100%"
          @click="handleUpload"
        >
          {{ loading ? '评估中（约 1-2 分钟）…' : '开始评估' }}
        </el-button>
      </el-form>

      <el-alert
        type="info"
        :closable="false"
        style="margin-top: 16px"
        title="五维度照护等级评定"
        description="ADL 日常生活活动能力、认知、情绪、社交、安全，加权综合评分 + 问题清单。"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules, type UploadFile } from 'element-plus'
import { User, UploadFilled } from '@element-plus/icons-vue'
import { assessmentApi } from '@/api/assessment'

const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)
const file = ref<File | null>(null)
const form = reactive({ site_id: '' })

const rules: FormRules = {
  site_id: [{ required: true, message: '请输入老人 ID', trigger: 'blur' }],
}

function handleFileChange(f: UploadFile) {
  file.value = (f.raw as File) ?? null
}

async function handleUpload() {
  if (!form.site_id || !file.value) return
  loading.value = true
  try {
    const { data } = await assessmentApi.upload(form.site_id, file.value)
    ElMessage.success('评估完成')
    router.push(`/assessment/${data.assessment_id}`)
  } catch (e) {
    console.error('[Assessment upload]', e)
    ElMessage.error('评估失败，请重试')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.upload-page {
  max-width: 640px;
}
.upload-card {
  border-radius: 8px;
}
.card-header {
  font-weight: 600;
}
</style>
