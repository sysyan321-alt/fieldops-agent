// scripts/capture_screenshots.mjs
// 自动截取 README 用的界面截图到 docs/images/。
//
// ⚠️ 状态：**未在真实环境验证过**（作者环境没有 playwright，且截图需要前端+后端+演示数据同时就绪）。
//    第一次运行时请盯一下输出，失败多半是选择器或等待时间的问题，改 3 处即可：
//    登录表单选择器、页面导航路径、waitForTimeout 的等待时长。
//
// 准备：
//   cd frontend && npm i -D playwright && npx playwright install chromium
//   # 前端起在 3000，后端起在 8000，且库里已有演示数据（./scripts/seed_data.py）
//
// 运行：
//   node scripts/capture_screenshots.mjs
//   BASE=http://localhost:3000 USER=supervisor PASS='Supervisor@123' node scripts/capture_screenshots.mjs
//
// 前置账号：默认用演示账号 supervisor（需在 .env.local 打开 SEED_DEMO_USERS=true 播种）。

import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const BASE = process.env.BASE ?? 'http://localhost:3000'
const USER = process.env.USER ?? 'supervisor'
const PASS = process.env.PASS ?? 'Supervisor@123'
const ELDER = process.env.ELDER ?? 's001'
const OUT = path.resolve('docs/images')

// 每张图要体现的信息决定了怎么截：这些界面里"数据"比"布局"更重要，
// 所以截整页（fullPage）而不是首屏，避免把进度表截了一半。
const SHOTS = [
  { name: 'service-plan', title: '照护计划页',
    why: '护理等级 + 服务项 + 今日执行进度 + 计划来源标签——最能说明这个项目在做什么',
    path: () => `/work-flow/${process.env.SESSION_ID ?? ''}/plan` },
  { name: 'alert-review', title: '告警审核页',
    why: '四轨证据 + AI 风险等级 + 站长确认按钮——体现 Human-in-the-Loop',
    path: () => '/alert-review' },
  { name: 'sop-qa', title: 'SOP 问答页',
    why: '回答带来源章节——体现"答案可追溯到 SOP 原文"',
    path: () => '/sop-qa' },
  { name: 'assessment', title: '评估上传页',
    why: '五维度评分入口——闭环的起点',
    path: () => '/assessment' },
]

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

  console.log(`→ 打开 ${BASE}`)
  await page.goto(BASE, { waitUntil: 'networkidle' })

  // 登录（前端是 Element Plus 表单，按 placeholder 定位最稳）
  if (page.url().includes('/login')) {
    console.log('→ 登录')
    await page.getByPlaceholder(/用户名|账号|username/i).fill(USER)
    await page.getByPlaceholder(/密码|password/i).fill(PASS)
    await page.getByRole('button', { name: /登录|登陆/ }).click()
    await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 15000 })
  }

  for (const shot of SHOTS) {
    const target = BASE + shot.path()
    console.log(`→ ${shot.title}：${target}`)
    try {
      await page.goto(target, { waitUntil: 'networkidle' })
      await page.waitForTimeout(1200)   // 等图表/表格渲染与异步数据
      const file = path.join(OUT, `${shot.name}.png`)
      await page.screenshot({ path: file, fullPage: true })
      console.log(`  ✓ ${file}`)
    } catch (e) {
      console.log(`  ✗ 跳过（${e.message.split('\n')[0]}）`)
      console.log(`    提示：${shot.title}需要先有数据——${shot.why}`)
    }
  }

  await browser.close()
  console.log('\n完成。把生成的图纳入版本控制：git add docs/images')
}

main().catch(e => {
  console.error('失败：', e.message)
  console.error('\n常见原因：')
  console.error('  1) 前端没起在 3000（cd frontend && npm run dev）')
  console.error('  2) 没有可用账号（在 .env.local 打开 SEED_DEMO_USERS=true 后重启后端）')
  console.error('  3) 库里没数据（python scripts/seed_data.py）')
  process.exit(1)
})
