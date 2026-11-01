#!/usr/bin/env bash
# Wave 4: Historical memory file migration
# Migrates old DMA output format to KW SPEC v1.0 compliant format
set -euo pipefail

MEMORY_DIR="${1:-$HOME/.openclaw/workspace/memory}"
BACKUP_DIR="${MEMORY_DIR}/.pre-wave4-backup-$(date +%Y%m%d-%H%M%S)"

echo "=== DMA Memory File Migration (Wave 4) ==="
echo "Memory dir: $MEMORY_DIR"
echo "Backup dir: $BACKUP_DIR"
echo ""

# Phase 0: Pause check
if [ -f "${MEMORY_DIR}/../skills/daily-memory-archiver/config/.archive.lock" ]; then
    echo "WARNING: DMA lock file exists (cron may be active)."
fi
if [ -t 0 ]; then
    echo "Press Enter to continue or Ctrl-C to abort..."
    read -r
else
    echo "Non-interactive mode: proceeding."
fi

# Phase 1: Backup
echo "[Phase 1] Creating backup..."
mkdir -p "$BACKUP_DIR"
cp "$MEMORY_DIR"/*.md "$BACKUP_DIR/" 2>/dev/null || true
echo "  Backed up $(ls "$BACKUP_DIR"/*.md 2>/dev/null | wc -l) files to $BACKUP_DIR"

# Phase 2: Migrate each file
echo "[Phase 2] Migrating files..."

migrated=0
for f in "$MEMORY_DIR"/*.md; do
    [ -f "$f" ] || continue
    basename=$(basename "$f")
    day="${basename%.md}"

    # Skip non-standard filenames
    if ! echo "$day" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
        echo "  SKIP $basename (non-standard filename)"
        continue
    fi

    changed=0

    # 2a. Replace old category titles with new ones (exact match in **bold**)
    # Order matters: replace longer/specific patterns before shorter ones to avoid partial matches
    sed -i \
        -e 's/\*\*项目与技术要点\*\*/\*\*技术\/项目要点\*\*/g' \
        -e 's/\*\*计划与待办\*\*/\*\*待办与计划\*\*/g' \
        -e 's/\*\*用户偏好\*\*/\*\*用户偏好与习惯\*\*/g' \
        -e 's/\*\*风险与注意\*\*/\*\*风险与注意事项\*\*/g' \
        -e 's/\*\*重要事实\*\*/\*\*核心要点\*\*/g' \
        "$f" && changed=1

    # 2b. Replace old error placeholders with DMA-ERR: format
    sed -i \
        -e 's/\*（云端摘要失败，见日志）\*/*DMA-ERR: cloud summary failed (see log)*/g' \
        -e 's/\*（段 \([0-9]*\)\/\([0-9]*\) 摘要失败）\*/*DMA-ERR: chunk \1\/\2 summary failed*/g' \
        "$f" && changed=1

    # 2c. Add YAML frontmatter if missing (file doesn't start with ---)
    if ! head -1 "$f" | grep -q '^---$'; then
        temp_file=$(mktemp)
        {
            echo "---"
            echo "title: \"${day} 会话记忆\""
            echo "date: \"${day}\""
            echo "---"
            echo ""
            cat "$f"
        } > "$temp_file"
        mv "$temp_file" "$f"
        changed=1
    fi

    if [ "$changed" -eq 1 ]; then
        echo "  MIGRATED $basename"
        migrated=$((migrated + 1))
    else
        echo "  UNCHANGED $basename"
    fi
done

echo "  Total migrated: $migrated files"

# Phase 3: Verify
echo "[Phase 3] Verifying migration..."

errors=0
for f in "$MEMORY_DIR"/*.md; do
    [ -f "$f" ] || continue
    basename=$(basename "$f")

    # Check no old error placeholders remain
    if grep -q '（云端摘要失败\|（段 [0-9]*/[0-9]* 摘要失败）' "$f" 2>/dev/null; then
        echo "  WARN: $basename still has old error placeholders"
        errors=$((errors + 1))
    fi

    # Check no old category titles remain
    if grep -qE '\*\*重要事实\*\*|\*\*计划与待办\*\*|\*\*项目与技术要点\*\*' "$f" 2>/dev/null; then
        echo "  WARN: $basename still has old category titles"
        errors=$((errors + 1))
    fi

    # Check frontmatter exists
    if ! head -1 "$f" | grep -q '^---$'; then
        echo "  WARN: $basename missing frontmatter"
        errors=$((errors + 1))
    fi
done

if [ "$errors" -eq 0 ]; then
    echo "  All files pass verification"
else
    echo "  $errors warnings found (non-blocking)"
fi

# Phase 4: Summary
echo ""
echo "=== Migration Complete ==="
echo "Backup: $BACKUP_DIR"
echo "To rebuild KW database: KNOWLEDGE_WEAVER_MEMORY_DIR=$MEMORY_DIR python -m knowledge_weaver consolidate"
echo "To rollback: rm $MEMORY_DIR/*.md && cp $BACKUP_DIR/*.md $MEMORY_DIR/"
