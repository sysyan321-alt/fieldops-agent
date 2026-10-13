# README 截图

公开 README 里的截图放这里。**目前是空的**——需要跑一次演示后截图。

## 需要的四张图

| 文件名 | 界面 | 要体现什么 |
|---|---|---|
| `service-plan.png` | 服务计划页（`/work-flow/:sid/plan`） | **最重要的一张**：风险等级、服务项、今日执行进度、计划来源标签 |
| `alert-review.png` | 告警审核页（`/alert-review`） | 四轨证据 + AI 风险等级 + 值班主管确认按钮（Human-in-the-Loop） |
| `sop-qa.png` | 规范问答页（`/sop-qa`） | 回答带来源章节（答案可追溯） |
| `assessment.png` | 点位评估上传页（`/assessment`） | 五维度评分入口（闭环起点） |

## 自动截图

```bash
cd frontend && npm i -D playwright && npx playwright install chromium
cd ..       && node scripts/capture_screenshots.mjs
```

脚本**未在真实环境验证过**（作者环境没有 playwright），第一次跑请盯输出；
失败通常是选择器或等待时长问题，改脚本头部 3 个地方即可。

## 手动截图也行

演示数据用 `python scripts/seed_data.py`，账号用 `SEED_DEMO_USERS=true` 播种，
然后：窗口宽度 **1440px**、整页截图（`fullPage`），这样表格不会被截断。

截完在图准备好之前，README 里的图片引用保持注释状态，避免出现坏图标。
