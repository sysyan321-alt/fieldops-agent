# 关于本目录的数据

## `sop/` — 作业规范示例文档

本目录下的 6 篇规范是**为本项目编写的示例内容**，用于演示检索链路与快通道门控，
**不是现场作业标准**，不能作为实际操作依据：

| 文件 | 文档 |
|---|---|
| `work_at_height.md` | 高处作业安全规范 |
| `power_room_inspection.md` | 配电间巡检规范 |
| `fire_equipment_check.md` | 消防设施检查规范 |
| `equipment_maintenance.md` | 设备保养与润滑规范 |
| `confined_space.md` | 受限空间作业规范 |
| `water_ingress.md` | 水浸与渗漏应急处置 |

真实部署时请替换为组织自有的作业规范，并注意：

- 若替换为第三方资料（教科书、行业标准、其他机构的文件），**请先确认授权**；
- 替换后需要重建索引：`python scripts/init_milvus.py` + `python scripts/build_knowledge_base.py --batch-dir data/sop`；
- 快通道索引同样要重建：`python scripts/build_quick_index.py`；
- 更新知识库后 `data/quick_index/golden*.jsonl` 里的回归集可能需要同步调整。

## `quick_index/` — SOP 快通道索引与回归集

| 文件 | 说明 |
|---|---|
| `entries.json` | 从 SOP 生成的索引条目（含 LLM 生成的相似问法） |
| `synonyms.json` | 口语 ↔ 术语映射表 |
| `overlay.json` | 人工补充的问法（不改 SOP 也能提高命中） |
| `golden.jsonl` | 调参集（开发时反复用过，容易过拟合，只作参考） |
| `golden_holdout.jsonl` | 留出集（调参时没用过，用来判断"换个问法还灵不灵"） |

回归评估：`python scripts/evaluate_quick_channel.py`
（报命中率、误命中率与判定耗时；**误命中必须保持 0**——宁可回落 RAG 慢一点，不可给错章节）。
