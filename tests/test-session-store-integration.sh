#!/usr/bin/env bash
# End-to-end storage and archive boundary checks. All state is run-scoped.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$(mktemp -d -t dma-session-store-integration.XXXXXX)"
trap 'rm -rf -- "$RUN_DIR"' EXIT

same_file() {
    python3 - "$1" "$2" <<'PY'
import pathlib, sys
sys.exit(0 if pathlib.Path(sys.argv[1]).read_bytes() == pathlib.Path(sys.argv[2]).read_bytes() else 1)
PY
}

failures=0
fail() { printf 'FAIL: %s\n' "$*" >&2; failures=$((failures + 1)); }
pass() { printf 'OK  : %s\n' "$*"; }

write_config() {
    local path="$1" min="$2" cloud="$3"
    mkdir -p "$(dirname "$path")"
    python3 - "$path" "$min" "$cloud" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_text(f'''openclaw:
  agent_id: main
session:
  key: ""
  merge_jsonl_keys:
    - "agent:main:main"
archive:
  trigger_mode: "scheduled"
  threshold:
    max_input_tokens: 400000
    check_interval_minutes: 5
    cooldown_minutes: 0
  min_new_messages: {int(sys.argv[2])}
  periodic_archive_minutes: 0
  compact_only_over_threshold: false
  compact:
    max_lines: 400
analyzer:
  messages_to_analyze: 50
  chunk_cloud_summary: false
  max_cloud_summary_chunks: 20
  substance_roles:
    - "user"
  cloud_summarizer:
    enabled: {sys.argv[3]}
    max_cloud_retry: 20
logging:
  log_max_bytes: 0
  log_keep_rotations: 2
  log_max_age_days: 0
output:
  memory_dir: "~/.openclaw/workspace/memory"
  raw_detail: "fallback_only"
skill_version: "1.7.1"
config_version: "8"
''', encoding="utf-8")
PY
}

make_jsonl() {
    local home="$1"
    python3 - "$home" <<'PY'
import json, pathlib, sys
home = pathlib.Path(sys.argv[1])
d = home / "agents" / "main" / "sessions"
d.mkdir(parents=True, exist_ok=True)
transcript = d / "session-main.jsonl"
events = [
 {"type":"session","version":3,"id":"session-main"},
 {"type":"message","id":"integration-user","timestamp":"2026-09-08T10:00:00.000Z",
  "message":{"role":"user","content":"请记录这条集成测试消息"}},
 {"type":"message","id":"integration-assistant","timestamp":"2026-09-08T10:01:00.000Z",
  "message":{"role":"assistant","content":"集成测试回复。"}},
 {"type":"message","id":"integration-tool","timestamp":"2026-09-08T10:02:00.000Z",
  "message":{"role":"toolResult","content":"ignored"}},
]
transcript.write_text("".join(json.dumps(e, ensure_ascii=False)+"\n" for e in events), encoding="utf-8")
(d / "sessions.json").write_text(json.dumps({"agent:main:main":{
 "sessionId":"session-main","updatedAt":1757320000000,"inputTokens":10,
 "totalTokens":20,"totalTokensFresh":True,"sessionFile":str(transcript)}}, ensure_ascii=False)+"\n", encoding="utf-8")
PY
}

make_sqlite() {
    local db="$1"
    python3 - "$db" <<'PY'
import json, pathlib, sqlite3, sys
dbpath = pathlib.Path(sys.argv[1])
dbpath.parent.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(dbpath)
try:
 con.executescript("""
 CREATE TABLE schema_meta(meta_key TEXT PRIMARY KEY, role TEXT NOT NULL, schema_version INTEGER NOT NULL,
   agent_id TEXT, app_version TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
 CREATE TABLE session_nodes(session_key TEXT PRIMARY KEY, current_session_id TEXT NOT NULL,
   entry_json TEXT NOT NULL, entry_valid INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL);
 CREATE TABLE session_windows(session_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
   previous_session_id TEXT, reason TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
   transcript_updated_at INTEGER, transcript_observed_at INTEGER);
 CREATE TABLE transcript_events(session_id TEXT NOT NULL, seq INTEGER NOT NULL,
   event_json TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(session_id,seq));
 CREATE TABLE transcript_event_identities(session_id TEXT NOT NULL, event_id TEXT NOT NULL,
   seq INTEGER NOT NULL, event_type TEXT, parent_id TEXT, message_idempotency_key TEXT,
   created_at INTEGER NOT NULL, PRIMARY KEY(session_id,event_id));
 """)
 con.execute("PRAGMA journal_mode=WAL")
 con.execute("PRAGMA user_version=19")
 con.execute("INSERT INTO schema_meta VALUES('primary','agent',19,'main','2026.9.2',1,1)")
 entry={"sessionId":"sqlite-current","updatedAt":1757320000000,
        "inputTokens":10,"totalTokens":20,"totalTokensFresh":True}
 con.execute("INSERT INTO session_nodes VALUES(?,?,?,1,?)",
             ("agent:main:main","sqlite-current",json.dumps(entry),entry["updatedAt"]))
 con.execute("INSERT INTO session_windows VALUES(?,?,?,?,?,?,?,?)",
             ("sqlite-current","agent:main:main",None,"initial",1,1,1,1))
 events=[
  {"type":"message","id":"integration-user","timestamp":"2026-09-08T10:00:00.000Z",
   "message":{"role":"user","content":"请记录这条集成测试消息"}},
  {"type":"message","id":"integration-assistant","timestamp":"2026-09-08T10:01:00.000Z",
   "message":{"role":"assistant","content":"集成测试回复。"}},
  {"type":"message","id":"integration-tool","timestamp":"2026-09-08T10:02:00.000Z",
   "message":{"role":"toolResult","content":"ignored"}}]
 for seq,e in enumerate(events,1):
  con.execute("INSERT INTO transcript_events VALUES(?,?,?,?)",
              ("sqlite-current",seq,json.dumps(e,ensure_ascii=False),seq))
  con.execute("INSERT INTO transcript_event_identities(session_id,event_id,seq,event_type,created_at) VALUES(?,?,?,?,?)",
              ("sqlite-current",e["id"],seq,e["type"],seq))
 con.commit()
 con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
finally:
 con.close()
PY
}

stub() {
    local path="$1" body="$2"
    python3 - "$path" "$body" <<'PY'
import pathlib, sys
p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(sys.argv[2], encoding="utf-8")
PY
    chmod +x "$path"
}
archive() {
    local engine="$1" out="$2" err="$3"
    bash "$engine" archive >"$out" 2>"$err"
}

echo "=== JSONL/SQLite Markdown equivalence and rerun ==="
P="$RUN_DIR/primary"; mkdir -p "$P"
export OPENCLAW_HOME="$P/openclaw"
export DAILY_MEMORY_CONFIG_DIR="$P/config"
export DAILY_MEMORY_MEMORY_DIR="$P/memory"
export DAILY_MEMORY_LOG="$P/dma.log"
export OPENCLAW_SKIP_COMPACT=1 SKIP_SESSION_COMPACT=1
export DAILY_MEMORY_SESSION_BACKEND=jsonl
unset DAILY_MEMORY_SQLITE_PATH
write_config "$P/config/config.yaml" 1 false
make_jsonl "$OPENCLAW_HOME"
if archive "$ROOT/scripts/archive-engine.sh" "$P/jsonl.out" "$P/jsonl.err"; then pass "JSONL archive runs"; else fail "JSONL archive failed: $(cat "$P/jsonl.err")"; fi
JSONL_MD="$(find "$P/memory" -maxdepth 1 -name '*.md' -type f -print -quit 2>/dev/null || true)"
JSONL_CP="$P/config/.archive_merge_checkpoint.json"
if [ -n "$JSONL_MD" ] && [ -f "$JSONL_CP" ]; then
 cp "$JSONL_MD" "$P/jsonl.md"; pass "JSONL writes Markdown/checkpoint"
else fail "JSONL did not write Markdown/checkpoint"; fi

rm -rf -- "$P/memory"; mkdir -p "$P/memory"; rm -f -- "$JSONL_CP"
DB="$OPENCLAW_HOME/agents/main/openclaw-agent.sqlite"
make_sqlite "$DB"; rm -f -- "$OPENCLAW_HOME/agents/main/sessions/sessions.json"
export DAILY_MEMORY_SESSION_BACKEND=sqlite DAILY_MEMORY_SQLITE_PATH="$DB"
if archive "$ROOT/scripts/archive-engine.sh" "$P/sqlite.out" "$P/sqlite.err"; then pass "SQLite archive works without sessions.json"; else fail "SQLite archive failed: $(cat "$P/sqlite.err")"; fi
SQLITE_MD="$(find "$P/memory" -maxdepth 1 -name '*.md' -type f -print -quit 2>/dev/null || true)"
if [ -n "$SQLITE_MD" ] && python3 - "$P/jsonl.md" "$SQLITE_MD" <<'PY'
import pathlib, re, sys
# A real archive can cross a minute boundary between backends.
def body(path):
    return re.sub(r"^## \d{2}:\d{2}$", "## <slot>", pathlib.Path(path).read_text(encoding="utf-8"), flags=re.M)
sys.exit(0 if body(sys.argv[1]) == body(sys.argv[2]) else 1)
PY
then pass "JSONL and SQLite Markdown match"; else fail "JSONL and SQLite Markdown differ"; fi
cp "$SQLITE_MD" "$P/sqlite.before-rerun.md"
cp "$P/config/.archive_merge_checkpoint.json" "$P/checkpoint.before-rerun"
if archive "$ROOT/scripts/archive-engine.sh" "$P/sqlite-rerun.out" "$P/sqlite-rerun.err" &&
   same_file "$P/checkpoint.before-rerun" "$P/config/.archive_merge_checkpoint.json" &&
   same_file "$P/sqlite.before-rerun.md" "$SQLITE_MD"; then
 pass "SQLite rerun is idempotent and keeps checkpoint stable"
else fail "SQLite rerun changed output/checkpoint"; fi

echo "=== cloud failure and min_new_messages gates ==="
F="$RUN_DIR/fault"; mkdir -p "$F/product" "$F/bin"
cp -R "$ROOT/scripts" "$F/product/scripts"
stub "$F/product/scripts/summarizers/cloud-summarizer.sh" '#!/usr/bin/env bash
printf "%s\n" invoked >>"$DMA_CLOUD_STUB_LOG"
exit 1
'
stub "$F/bin/openclaw" '#!/usr/bin/env bash
printf "%s\n" "$*" >>"$DMA_COMPACT_STUB_LOG"
exit 0
'
export PATH="$F/bin:$PATH"
export DMA_CLOUD_STUB_LOG="$F/cloud.log" DMA_COMPACT_STUB_LOG="$F/compact.log"
export SKIP_SESSION_COMPACT=0 OPENCLAW_SKIP_COMPACT=0
export DAILY_MEMORY_API_URL="http://127.0.0.1:9/never" DAILY_MEMORY_API_TOKEN=fixture DAILY_MEMORY_MODEL=fixture
export OPENCLAW_HOME="$F/openclaw" DAILY_MEMORY_CONFIG_DIR="$F/config" DAILY_MEMORY_MEMORY_DIR="$F/memory" DAILY_MEMORY_LOG="$F/dma.log"
export DAILY_MEMORY_SESSION_BACKEND=jsonl
write_config "$F/config/config.yaml" 1 true
make_jsonl "$OPENCLAW_HOME"
printf '%s\n' '{"agent:main:main":"2026-09-08T09:00:00.000Z"}' >"$F/config/.archive_merge_checkpoint.json"
cp "$F/config/.archive_merge_checkpoint.json" "$F/checkpoint.before"
if archive "$F/product/scripts/archive-engine.sh" "$F/cloud.out" "$F/cloud.err"; then pass "cloud failure returns cleanly"; else fail "cloud failure returned nonzero: $(cat "$F/cloud.err")"; fi
if [ ! -e "$F/memory/$(date +%Y-%m-%d).md" ] && same_file "$F/checkpoint.before" "$F/config/.archive_merge_checkpoint.json"; then
 pass "cloud failure does not write memory/checkpoint"
else fail "cloud failure changed memory/checkpoint"; fi
[ -s "$F/cloud.log" ] && pass "cloud failure stub was invoked" || fail "cloud failure stub was not invoked"
[ ! -s "$F/compact.log" ] && pass "cloud failure does not compact" || fail "cloud failure unexpectedly compacted"

D="$RUN_DIR/deferred"; mkdir -p "$D"
export OPENCLAW_HOME="$D/openclaw" DAILY_MEMORY_CONFIG_DIR="$D/config" DAILY_MEMORY_MEMORY_DIR="$D/memory" DAILY_MEMORY_LOG="$D/dma.log"
export DAILY_MEMORY_SESSION_BACKEND=jsonl
export DAILY_MEMORY_API_URL="" DAILY_MEMORY_API_TOKEN="" DAILY_MEMORY_MODEL=""
rm -f -- "$F/compact.log"
write_config "$D/config/config.yaml" 3 false
make_jsonl "$OPENCLAW_HOME"
printf '%s\n' '{"agent:main:main":"2026-09-08T09:00:00.000Z"}' >"$D/config/.archive_merge_checkpoint.json"
cp "$D/config/.archive_merge_checkpoint.json" "$D/checkpoint.before"
if archive "$ROOT/scripts/archive-engine.sh" "$D/defer.out" "$D/defer.err"; then pass "min_new_messages deferral returns cleanly"; else fail "deferral returned nonzero: $(cat "$D/defer.err")"; fi
if [ ! -e "$D/memory/$(date +%Y-%m-%d).md" ] && same_file "$D/checkpoint.before" "$D/config/.archive_merge_checkpoint.json"; then
 pass "deferred batch does not write memory/checkpoint"
else fail "deferred batch changed memory/checkpoint"; fi
[ ! -s "$F/compact.log" ] && pass "deferred batch does not compact" || fail "deferred batch unexpectedly compacted"

echo "=== interactive SQLite discovery and failure status ==="
export DAILY_MEMORY_SESSION_BACKEND=sqlite DAILY_MEMORY_SQLITE_PATH="$DB"
export SKIP_SESSION_COMPACT=1 OPENCLAW_SKIP_COMPACT=1
if bash "$ROOT/bin/daily-memory-archiver" interactive list-sessions main >"$D/list.json" &&
   jq -e '.count == 1 and .sessions[0].key == "agent:main:main"' "$D/list.json" >/dev/null; then
 pass "interactive lists SQLite-only sessions"
else fail "interactive SQLite list failed"; fi
python3 - "$DB" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("PRAGMA user_version=20")
PY
if bash "$ROOT/bin/daily-memory-archiver" interactive archive force >"$D/interactive.json" 2>"$D/interactive.err"; then
 fail "interactive swallowed an unsupported-schema failure"
elif jq -e '.ok == false and .exit_code != 0' "$D/interactive.json" >/dev/null &&
     same_file "$D/checkpoint.before" "$D/config/.archive_merge_checkpoint.json"; then
 pass "interactive reports schema failure without advancing checkpoint"
else fail "interactive failure envelope/checkpoint mismatch"; fi

echo "=== empty legacy bootstrap then late append ==="
B="$RUN_DIR/bootstrap"; mkdir -p "$B"
export OPENCLAW_HOME="$B/openclaw" DAILY_MEMORY_CONFIG_DIR="$B/config" DAILY_MEMORY_MEMORY_DIR="$B/memory" DAILY_MEMORY_LOG="$B/dma.log"
export DAILY_MEMORY_SESSION_BACKEND=jsonl
unset DAILY_MEMORY_SQLITE_PATH
write_config "$B/config/config.yaml" 1 false
make_jsonl "$OPENCLAW_HOME"
printf '%s\n' '{"agent:main:main":"2026-09-08T10:01:00.000Z"}' >"$B/config/.archive_merge_checkpoint.json"
if archive "$ROOT/scripts/archive-engine.sh" "$B/empty.out" "$B/empty.err" &&
   jq -e '.["agent:main:main"].version == 2' "$B/config/.archive_merge_checkpoint.json" >/dev/null &&
   [ ! -e "$B/memory/$(date +%Y-%m-%d).md" ]; then
 pass "empty legacy run commits identity bootstrap without memory"
else fail "empty bootstrap did not commit v2 safely"; fi
python3 - "$OPENCLAW_HOME/agents/main/sessions/session-main.jsonl" <<'PY'
import json, pathlib, sys
with pathlib.Path(sys.argv[1]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"type":"message","id":"late-after-bootstrap","timestamp":"2026-09-08T10:00:00.000Z","message":{"role":"user","content":"late bootstrap event must survive"}})+"\n")
PY
if archive "$ROOT/scripts/archive-engine.sh" "$B/late.out" "$B/late.err" &&
   grep -q 'late bootstrap event must survive' "$B/memory/$(date +%Y-%m-%d).md"; then
 pass "late message before old timestamp is archived after bootstrap"
else fail "late message was lost after bootstrap"; fi

if [ "$failures" -eq 0 ]; then echo "ALL session-store integration checks PASS"; else printf '%s integration check(s) failed\n' "$failures" >&2; fi
exit "$failures"
