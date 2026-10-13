#!/usr/bin/env bash
# scripts/export_public.sh
# 把 fieldops 导出成一份可以公开的副本（白名单导出 + 泄漏扫描）。
#
# 为什么是"导出"而不是"复制一份再手工删"：
#   手工维护两份会漂移——改完这边忘了那边，公开那份很快过期；
#   而 .gitignore 只兜得住"没被 add 的文件"，兜不住"别人 clone 后能看到什么"。
#   所以公开仓库由本脚本生成，**不要手工改公开那份**。
#
# 用法：
#   ./scripts/export_public.sh                      # 导出到 ../fieldops-public
#   ./scripts/export_public.sh --out /tmp/pub       # 指定输出目录（试跑用）
#   ./scripts/export_public.sh --dry-run            # 只打印将导出/排除的清单
#   ./scripts/export_public.sh --allow-warnings     # 泄漏扫描有"疑似"命中时仍继续
#
# 退出码：0=成功；1=参数/环境错误；2=泄漏扫描命中（已中止，输出目录保持原样未提交）

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$SRC/../fieldops-public"
DRY_RUN=0
ALLOW_WARNINGS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --allow-warnings) ALLOW_WARNINGS=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

# ── 顶层白名单：只有这些会进公开仓库 ──────────────────────────
INCLUDE=(
  backend
  frontend
  scripts
  tests
  data
  README.md
  README.public.md
  LICENSE
  CHANGELOG.md
  .github
  requirements.txt
  pytest.ini
  docker-compose.yml
  .gitignore
  .env.example
  文档
  docs
)

# 公开仓库里也要排除的私有文档（个人职业材料 / 另一个项目 / 课程材料）
EXCLUDE_PRIVATE=(
  文档/当前项目面试题
  文档/项目总结-按迁移矩阵.md
  文档/面试问答-Agent专题.md
  文档/FieldOps-项目完整文档.md
  文档/医小助-项目完整文档.md
  语音输入还没做.md
)

# 目录/文件级排除（体积、本地配置、工具产物）
RSYNC_EXCLUDES=(
  --exclude=node_modules/
  --exclude=dist/
  --exclude=__pycache__/
  --exclude=*.pyc
  --exclude=.pytest_cache/
  --exclude=.idea/
  --exclude=.jbeval/
  --exclude=.DS_Store
  --exclude=backend/models/          # 4.2G 模型权重，公开仓库靠 README 指引下载
  --exclude=.env.local
  --exclude=.env.*.local
  --exclude=*.log
)

say() { printf '%s\n' "$*"; }

# ── ① 展示清单 ────────────────────────────────────────────────

say "源目录：$SRC"
say "输出目录：$OUT"
say ""
say "【将导出】"
for item in "${INCLUDE[@]}"; do
  [[ -e "$SRC/$item" ]] && say "  ✓ $item" || say "  - $item（源目录不存在，跳过）"
done
say ""
say "【明确排除】"
for item in "${EXCLUDE_PRIVATE[@]}"; do
  [[ -e "$SRC/$item" ]] && say "  ✗ $item"
done
say "  ✗ backend/models/（4.2G 权重）、node_modules/、dist/、__pycache__/、.idea/、.jbeval/、.env.local"
say ""

if [[ $DRY_RUN -eq 1 ]]; then
  say "（--dry-run：未执行导出）"
  exit 0
fi

# ── ② 导出 ────────────────────────────────────────────────────

command -v rsync >/dev/null || { echo "需要 rsync" >&2; exit 1; }

mkdir -p "$OUT"
say "开始导出 → $OUT"
for item in "${INCLUDE[@]}"; do
  [[ -e "$SRC/$item" ]] || continue
  rsync -a --delete-excluded "${RSYNC_EXCLUDES[@]}" "$SRC/$item" "$OUT/" 2>/dev/null \
    || rsync -a "${RSYNC_EXCLUDES[@]}" "$SRC/$item" "$OUT/"
done

# 私有文档：导出后兜底再删一次（防止白名单调整时漏掉）
for item in "${EXCLUDE_PRIVATE[@]}"; do
  rm -rf "$OUT/$item" 2>/dev/null || true
done
rm -rf "$OUT/backend/models" "$OUT/frontend/node_modules" "$OUT/frontend/dist" 2>/dev/null || true

# 公开 README 覆盖：私有 README 会引用只在私有仓库里存在的文档（面试/简历套件），
# 那些链接在公开仓库里会 404。存在 README.public.md 时用它替换。
if [[ -f "$SRC/README.public.md" ]]; then
  cp "$SRC/README.public.md" "$OUT/README.md"
  rm -f "$OUT/README.public.md"      # 公开仓库不该留这份"私有 README 的替代稿"
  say "已用 README.public.md 覆盖公开仓库的 README"
else
  say "⚠️  未找到 README.public.md：沿用私有 README（其中指向私有文档的链接在公开仓库会失效）"
fi

say "导出完成：$(du -sh "$OUT" | cut -f1)（源目录 $(du -sh "$SRC" | cut -f1)）"
say ""

# ── ③ 泄漏扫描 ────────────────────────────────────────────────
# 分两级：FATAL 必须解决才能公开；WARN 需要人判断。

say "泄漏扫描中……"

FATAL_PATTERNS=(
  'sk-[A-Za-z0-9_-]{10,}'                    # LLM API Key
  'tvly-[A-Za-z0-9_-]{10,}'                  # Tavily Key
  'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'   # GitHub Token
  'AKIA[0-9A-Z]{16}'                         # AWS Access Key ID
  'xox[baprs]-[A-Za-z0-9-]{10,}'             # Slack Token
  'eyJ[A-Za-z0-9_-]{20,}\.'                  # JWT
  'BEGIN [A-Z ]*PRIVATE KEY'                 # 私钥
  'password\s*[:=]\s*["'"'"'][^"'"'"']{6,}'  # 明文口令
)

WARN_PATTERNS=(
  '1[3-9][0-9]{9}'                           # 手机号
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|cn|net|org)'
  '192\.168\.[0-9]+\.[0-9]+|10\.254\.[0-9]+\.[0-9]+'   # 具体的内网地址（x.x 形式的示例不算）
  '/Users/song'                              # 本机绝对路径
  '简历|自述稿|三档版本|迁移矩阵|语雀|HR 面'   # 个人职业材料
  '面试'                                     # 面试向措辞（公开文档里不该出现）
)

# 本地私有模式：任职公司名、姓名、客户名等**不应该出现在公开仓库里**的词。
# 它们放在一个不进版本控制的本地文件里，而不是硬编码进本脚本——
# 否则脚本自身就把这些词带进了公开仓库（这一点我自己踩过）。
EXTRA_PATTERNS_FILE="$SRC/.leak-scan-extra"
if [[ -f "$EXTRA_PATTERNS_FILE" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    WARN_PATTERNS+=("$line")
  done < "$EXTRA_PATTERNS_FILE"
  say "已加载本地私有扫描词（${EXTRA_PATTERNS_FILE}，$(grep -cvE '^\s*$|^#' "$EXTRA_PATTERNS_FILE") 条）"
else
  say "⚠️  未找到 ${EXTRA_PATTERNS_FILE}：公司名/客户名/姓名等私有词不会被扫描。"
  say "    建议创建它（每行一个正则），内容不进版本控制。"
fi

SCAN_GLOBS=(--include='*.py' --include='*.ts' --include='*.vue' --include='*.md'
            --include='*.json' --include='*.sql' --include='*.sh' --include='*.yml'
            --include='*.example' --include='*.txt' --include='*.ini')

# 扫描时要跳过文件：
#   export_public.sh —— 它自己就写着这些模式字面量，必然自命中
# 占位符过滤：.env.example 里的 sk-xxxxxxxx 是给人看的模板，不是真密钥
SCAN_SKIP=(--exclude-dir=node_modules --exclude-dir=dist --exclude-dir=__pycache__
           --exclude=export_public.sh)
PLACEHOLDER_FILTER='sk-x{4,}|sk-your|sk-changeme|sk-\*|sk-<|xxx+|your-|-here|changeme'

fatal_hits=0
warn_hits=0

scan() {
  local label="$1"; shift
  local pattern="$1"; shift
  local filter="${1:-}"
  local out
  out="$(grep -rInE "${SCAN_GLOBS[@]}" "${SCAN_SKIP[@]}" "$pattern" "$OUT" 2>/dev/null || true)"
  if [[ -n "$out" && -n "$filter" ]]; then
    out="$(printf '%s\n' "$out" | grep -vE "$filter" || true)"
  fi
  if [[ -n "$out" ]]; then
    say ""
    say "[$label] 命中模式：$pattern"
    printf '%s\n' "$out" | sed "s|$OUT/|  |" | head -40
    local n; n=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
    [[ "$n" -gt 40 ]] && say "  … 共 $n 行"
    if [[ "$label" == "FATAL" ]]; then fatal_hits=$((fatal_hits+1)); else warn_hits=$((warn_hits+1)); fi
  fi
}

for p in "${FATAL_PATTERNS[@]}"; do scan FATAL "$p" "$PLACEHOLDER_FILTER"; done
for p in "${WARN_PATTERNS[@]}";  do scan WARN  "$p"; done

say ""
if [[ $fatal_hits -gt 0 ]]; then
  say "❌ 泄漏扫描发现 $fatal_hits 类致命命中（密钥/私钥/明文口令）——已中止，请先清理源仓库后再导出。"
  exit 2
fi
if [[ $warn_hits -gt 0 && $ALLOW_WARNINGS -eq 0 ]]; then
  say "⚠️  泄漏扫描有 $warn_hits 类疑似命中（手机号/邮箱/公司名/内网地址/个人材料）。"
  say "    逐条确认无误后加 --allow-warnings 重跑；确认不了的先清理源仓库。"
  exit 2
fi
say "✅ 泄漏扫描通过（致命 0 类，疑似 $warn_hits 类）"
say ""
say "下一步："
say "  cd \"$OUT\" && git init && git add -A && git commit -m 'chore: initial public release'"
say "  gh repo create fieldops-agent --public --source=. --push"
