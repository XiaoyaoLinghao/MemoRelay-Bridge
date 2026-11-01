#!/usr/bin/env bash
# Daily Memory Archiver — 日志按大小轮转 + 按天龄删除旧轮转文件
# 由 archive-engine.sh source；也可单独测试：source 后调用下列函数。
set -euo pipefail

# 按天龄删除：仅删除「轮转备份」$(basename).1、.2…，不删当前活动日志。
# 返回 stdout：删除的文件个数（单行数字）。
daily_memory_prune_logs_by_age() {
    local logf="$1" max_age="${2:-0}"
    if ! [ "$max_age" -gt 0 ] 2>/dev/null; then
        echo 0
        return 0
    fi
    local dir base deleted=0
    dir=$(dirname "$logf")
    base=$(basename "$logf")
    local fp
    while IFS= read -r -d '' fp; do
        rm -f "$fp"
        deleted=$((deleted + 1))
    done < <(find "$dir" -maxdepth 1 -type f -name "${base}.*" -mtime +"$max_age" -print0 2>/dev/null)
    echo "$deleted"
}

# 当前日志超过 max_bytes 时轮转：.1←当前，旧 .k→.k+1，最多保留 keep 个备份（.1…keep）。
# 返回：0=已轮转，1=未触发
daily_memory_rotate_log_chain() {
    local f="$1" max_bytes="${2:-0}" keep="${3:-5}"
    if ! [ "$max_bytes" -gt 0 ] 2>/dev/null; then
        return 1
    fi
    [ -f "$f" ] || return 1
    local sz
    sz=$(wc -c <"$f" 2>/dev/null || echo 0)
    if ! [ "$sz" -gt "$max_bytes" ] 2>/dev/null; then
        return 1
    fi
    [ "$keep" -lt 1 ] && keep=5
    local i
    [ -f "${f}.${keep}" ] && rm -f "${f}.${keep}"
    i=$((keep - 1))
    while [ "$i" -ge 1 ]; do
        if [ -f "${f}.$i" ]; then
            mv "${f}.$i" "${f}.$((i + 1))"
        fi
        i=$((i - 1))
    done
    mv "$f" "${f}.1"
    : >"$f"
    return 0
}
