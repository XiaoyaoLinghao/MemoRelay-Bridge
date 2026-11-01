#!/usr/bin/env bash
# DMA 健康检查：扫描最近 N 天 memory，检测漏档风险，异常则告警。
# 用法: bash scripts/health-check.sh [--days N]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="${DAILY_MEMORY_CONFIG_DIR:-$SKILL_ROOT/config}"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
LOG_FILE="${DAILY_MEMORY_LOG:-$OPENCLAW_HOME/logs/daily-memory-archiver.log}"

# 读 memory_dir（参照 archive-engine.sh 的 load_config）
MEMORY_DIR="${DAILY_MEMORY_MEMORY_DIR:-$OPENCLAW_HOME/workspace/memory}"
MEMORY_DIR="${MEMORY_DIR/#\~/$HOME}"

DAYS=3
[ "${1:-}" = "--days" ] && DAYS="${2:-3}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"; }

problems=()

# 1. 检查 retry 计数（持续失败的强信号）
if [ -f "$CONFIG_DIR/.cloud_retry_count" ]; then
    rc=$(cat "$CONFIG_DIR/.cloud_retry_count" 2>/dev/null || echo 0)
    if [ "${rc:-0}" -ge 3 ] 2>/dev/null; then
        problems+=("云端摘要连续失败 ${rc} 次（cloud API key/凭据可能失效）")
    fi
fi

# 2. 检查强制归档告警标记
if [ -f "$CONFIG_DIR/.cloud_fail_alert" ]; then
    problems+=("已触发强制归档（云端失败超上限），部分会话以 DMA-ERR 占位归档，存在漏档")
fi

# 3. 扫最近 N 天 memory 文件的 DMA-ERR 密度
today_epoch=$(date +%s)
for i in $(seq 0 $((DAYS - 1))); do
    d=$(date -d "@$((today_epoch - i*86400))" +%Y-%m-%d 2>/dev/null || date -r $((today_epoch - i*86400)) +%Y-%m-%d)
    f="$MEMORY_DIR/${d}.md"
    [ -f "$f" ] || continue
    total_slots=$(grep -c '^## ' "$f" 2>/dev/null | tr -d '[:space:]' || echo 0)
    [ -z "$total_slots" ] && total_slots=0
    err_slots=$(grep -c 'DMA-ERR' "$f" 2>/dev/null | tr -d '[:space:]' || echo 0)
    [ -z "$err_slots" ] && err_slots=0
    # 若某天有内容槽但 DMA-ERR 占比过半，提示
    if [ "$total_slots" -gt 0 ] && [ "$err_slots" -gt 0 ]; then
        # 简单阈值：DMA-ERR 行数 >= 时间槽数（多数槽失败）
        if [ "$err_slots" -ge "$total_slots" ]; then
            problems+=("${d}: ${total_slots} 个时段全部为 DMA-ERR（疑似整天漏档）")
        fi
    fi
done

if [ "${#problems[@]}" -eq 0 ]; then
    echo "OK: DMA health check passed (last ${DAYS} days)"
    exit 0
fi

# 有问题：写日志 + 标准输出
msg="DMA 健康检查发现 ${#problems[@]} 个问题："
for p in "${problems[@]}"; do msg="${msg}"$'\n'"  - ${p}"; done
echo "$msg"
log "[ALERT] $msg"

# 可选：通过 openclaw 推送飞书（若 CLI 可用且配置了通道）
if command -v openclaw >/dev/null 2>&1 && [ "${DMA_HEALTH_PUSH:-0}" = "1" ]; then
    # 推送实现按 openclaw 通道配置补充；默认不推，避免误发
    :
fi

exit 1
