#!/usr/bin/env bash
# scripts/check_leaks.sh
# 推送前自查：扫描仓库里是否混进密钥、口令、个人路径，或上一手项目的残留词。
#
# 为什么需要：开源仓库的常见事故不是代码写错，而是把 .env、私钥、内部系统名
# 或作者本机路径一起提交了。把"推之前扫一遍"变成一条命令，成本最低。
#
# 用法：
#   ./scripts/check_leaks.sh                    # 扫描仓库
#   ./scripts/check_leaks.sh --allow-warnings    # 有"疑似"命中时也放行
#
# 退出码：0=通过；2=命中（需人工确认后再推）
#
# 可选的本地私有词表 .leak-scan-extra（每行一个正则，**不进版本控制**）：
# 用来补充只在你环境里有意义的词——任职公司名、客户名、本人姓名等。
# 这些词不能硬编码进本脚本，否则脚本自身就把它们带进了公开仓库。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOW_WARNINGS=0
[[ "${1:-}" == "--allow-warnings" ]] && ALLOW_WARNINGS=1

SKIP=(--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist
      --exclude-dir=__pycache__ --exclude-dir=.pytest_cache --exclude=self
      --exclude=check_leaks.sh --exclude-dir=backend/models
      --exclude=SCENARIO.md)   # 术语表本来就该出现旧名（它是映射表）
GLOBS=(--include='*.py' --include='*.ts' --include='*.vue' --include='*.md' --include='*.json'
       --include='*.sql' --include='*.sh' --include='*.yml' --include='*.example' --include='*.txt'
       --include='*.ini' --include='*.mjs' --include='*.html' --include='*.css')

FATAL=(
  'sk-[A-Za-z0-9_-]{10,}'                  # LLM API Key
  'tvly-[A-Za-z0-9_-]{10,}'                # Tavily
  'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
  'AKIA[0-9A-Z]{16}'                       # AWS
  'xox[baprs]-[A-Za-z0-9-]{10,}'           # Slack
  'eyJ[A-Za-z0-9_-]{20,}\.'                # JWT
  'BEGIN [A-Z ]*PRIVATE KEY'
  'password\s*[:=]\s*["'"'"'][^"'"'"']{6,}'
)
# 占位符过滤：.env.example 里的 sk-xxxx 是模板，不是真密钥
PLACEHOLDER='sk-x{4,}|sk-your|sk-changeme|sk-\*|sk-<|xxx+|your-|-here|changeme|<owner>|<repo>'

WARN=(
  '1[3-9][0-9]{9}'                                       # 手机号
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|cn|net|org)'   # 邮箱
  '192\.168\.[0-9]+\.[0-9]+|10\.254\.[0-9]+\.[0-9]+'     # 具体内网地址
  '/Users/[A-Za-z]+'                                     # 作者本机绝对路径
  # 上一手项目的残留词：本项目是脱域改造而来，这些词留在这里说明没改干净
  '老人|护理|照护|养老|鼻饲|压疮|离床|卧床|喂药|翻身的'
  'care_agent|CareAgent|care-flow|care_plan|care_flow'
)

EXTRA="$ROOT/.leak-scan-extra"
if [[ -f "$EXTRA" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    WARN+=("$line")
  done < "$EXTRA"
  echo "已加载本地私有词表 ${EXTRA}"
else
  echo "（未找到 .leak-scan-extra：公司名/客户名/本人姓名等私有词不会被扫描）"
fi

echo "扫描 $ROOT ..."
fatal_hits=0; warn_hits=0
scan() {
  local label="$1" pattern="$2" filter="${3:-}" out
  out="$(grep -rInE "${GLOBS[@]}" "${SKIP[@]}" "$pattern" "$ROOT" 2>/dev/null || true)"
  [[ -n "$out" && -n "$filter" ]] && out="$(printf '%s\n' "$out" | grep -vE "$filter" || true)"
  if [[ -n "$out" ]]; then
    echo ""; echo "[$label] 命中：$pattern"
    printf '%s\n' "$out" | sed "s|$ROOT/|  |" | head -30
    if [[ "$label" == "FATAL" ]]; then fatal_hits=$((fatal_hits+1)); else warn_hits=$((warn_hits+1)); fi
  fi
}
for p in "${FATAL[@]}"; do scan FATAL "$p" "$PLACEHOLDER"; done
for p in "${WARN[@]}";  do scan WARN  "$p"; done

echo ""
if [[ $fatal_hits -gt 0 ]]; then
  echo "❌ 致命命中 $fatal_hits 类（密钥/私钥/明文口令）——不要推，先清理。"; exit 2
fi
if [[ $warn_hits -gt 0 && $ALLOW_WARNINGS -eq 0 ]]; then
  echo "⚠️  疑似命中 $warn_hits 类（手机号/邮箱/内网地址/本机路径/上一手项目残留词）。"
  echo "    逐条确认无误后加 --allow-warnings 重跑。"; exit 2
fi
echo "✅ 自查通过（致命 0 类，疑似 $warn_hits 类）"
