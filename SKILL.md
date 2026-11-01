---
name: daily-memory-archiver
skill_version: "1.7.1"
config_version: "8"
spec_version: "1.1"
description: |
  Daily Memory Archiver v1.7.1 — OpenClaw 会话归档：按 session key 统计用量、检查点后仅合并新增消息、可选分块云端摘要、默认仅对超限 key 执行 sessions.compact，并发布供健康检查消费的运行状态证据。推荐根目录 ~/.openclaw/workspace/skills/daily-memory-archiver（自维护）；亦支持 ~/.openclaw/skills/daily-memory-archiver（全局）。

  **必须读取本 Skill 时**：安装/配置 API、定时归档、credentials.enc、merge_jsonl_keys、检查点、pairing、多通道、get-cloud-creds、archive-engine。

  **Skill 根目录（推荐）**：`~/.openclaw/workspace/skills/daily-memory-archiver` — 与 `agents.defaults.workspace` 同级工作区 skills，便于 Git / 备份 / 迁移。

  **维护文档**：同目录 `README.md`（工程布局、日志策略、改代码清单）。
---

# Daily Memory Archiver

**文档与实现版本：1.7.1**（版本唯一来源为本文件 frontmatter；`config.yaml` 与正文同步镜像。）

原生存储适配、检查点升级与回退边界见 [升级说明](docs/sqlite-upgrade.md)。需要 Python 3.9+（含 sqlite3）。

## 0. 核心思路

1. **对话驱动**：由助手执行脚本，避免用户死记命令。
2. **敏感信息**：`api_url` / `api_token` / `model` 仅入 **`config/credentials.enc`**；回复中禁止复述完整 Token。
3. **单次入口**：`scripts/archive-engine.sh archive` 或 `bin/daily-memory-archiver archive`。整体流程：**通过统一适配器读取 SQLite / JSONL → 按 key 统计用量 → 判断是否应运行 → 自检查点起仅合并各 key 的新消息（跨 key 按 `timestamp` 排序）→ 本地提取 + 可选云端摘要（可分段）→ 写 `memory/YYYY-MM-DD.md` → 仅对用量达阈值的 key 调用 `sessions.compact`（可配置）**。
4. **与轻量 memory 钩子可并存**。

### 0.1 安装根目录：`~/.openclaw/skills/` 与 `~/.openclaw/workspace/skills/`

| 位置 | 典型用途 |
|:---|:---|
| **`$HOME/.openclaw/skills/`** | OpenClaw **全局** skills，核心或官方分发常见于此。 |
| **`$HOME/.openclaw/workspace/skills/`** | **工作区** skills（与 `openclaw.json` → `agents.defaults.workspace` 对应目录下的 `skills/`），适合**自编写、Git 管理、随仓库迁移**的本 skill。 |

**本文命令与 crontab 示例**以 **`$HOME/.openclaw/workspace/skills/daily-memory-archiver`** 为准。若你仍安装在全局目录，把路径前缀改为 **`$HOME/.openclaw/skills/daily-memory-archiver`** 即可。  
`archive-engine.sh` / `config-manager.sh` 通过**脚本所在目录**解析 `SKILL_ROOT` 与默认 `config/`，**不依赖**安装在上表哪一侧；也可用环境变量 **`DAILY_MEMORY_CONFIG_DIR`** 指向另一份 `config/`（高级用法）。

**从全局迁到工作区**：见本文 **§9**。

### 0.2 与 Knowledge Weaver 的契约

**Knowledge Weaver (KW)** 从本 skill 写入的 `memory/YYYY-MM-DD.md` 文件中提取知识实体。
KW 维护一份规范文档 `KW_MEMORY_FILE_SPEC.md`（v1.1），本 skill 的输出必须严格遵守。

**本 skill 必须保证**：

1. **文件名**：`YYYY-MM-DD.md`，date 与文件名 ISO 8601 一致
2. **frontmatter**：写入 `title` 与 `date` 字段（SPEC §3.1）
3. **分类标题**（SPEC §4）必须**逐字符匹配**以下字符串：
   - `**核心要点**`     → fact
   - `**决策与结论**`   → decision
   - `**已完成事项**`   → task
   - `**待办与计划**`   → task
   - `**用户偏好与习惯**` → preference
   - `**技术/项目要点**`  → tech
   - `**风险与注意事项**` → risk
   - `**创意与想法**`   → idea
   - `**关键讨论**`     → fact（v1.0 扩展分类，仅 local-extractor 使用）
4. **失败占位符**（SPEC §6）：使用 `- *DMA-ERR: <reason>*` 格式

**修改约束**：

- 修改任意分类标题字符串前，必须确认 KW SPEC 当前版本支持新标题
- 修改 `scripts/extractors/local-extractor.sh` 或 `scripts/summarizers/cloud-summarizer.sh` 中的 `**xxx**` 标题，必须更新本节并跑 `scripts/test-extractor-titles.sh`
- 修改失败占位符格式，必须协调 KW 仓库 `_GARBAGE_PATTERNS` 同步增加

## 1. 依赖

`bash`（建议 4.x，使用关联数组）、`jq`、`openssl`、`curl`；推荐 `flock`、`openclaw`（compact）。

```bash
bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/self-check.sh"
```

## 2. 安装与 API

```bash
"$HOME/.openclaw/workspace/skills/daily-memory-archiver/bin/daily-memory-archiver" init --defaults
```

写入凭证（JSON）：

```bash
jq -n --arg u "BASE_URL" --arg t "TOKEN" --arg m "MODEL" \
  '{api_url:$u, api_token:$t, model:$m}' | \
  bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/config-manager.sh" save-json
```

**`save-json` 会保留**已有 **`session.key`** 与 **`merge_jsonl_keys`**，不会清空多通道列表。

状态：`config-manager.sh status` / `show`（脱敏）。

### 2.1 配置 `output.memory_dir`

`config.yaml` 中 `output.memory_dir` 控制 memory 文件（`YYYY-MM-DD.md`）的写入目录。

- **默认值**：`~/.openclaw/workspace/memory`（`~` 会在运行时展开为当前用户的 `$HOME`，不会泄露具体路径）。
- **自定义**：OpenClaw 对话配置时应询问用户的实际 memory 目录，若用户无特殊要求则保留默认值。可通过以下方式覆盖：
  - 直接编辑 `config.yaml`：`memory_dir: "/你的自定义路径"`
  - 环境变量：`DAILY_MEMORY_MEMORY_DIR=/你的自定义路径`
- **注意**：请勿在 `config.yaml` 中填入其他用户的绝对路径（如 `/home/某用户/…`），该文件可能被分享或提交。始终使用 `~` 或环境变量。

## 3. 归档命令

```bash
bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/archive-engine.sh" archive
bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/archive-engine.sh" archive --force
# 可选：只处理单个 session（覆盖 merge 列表）
bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/archive-engine.sh" archive --session 'agent:main:main'
bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/archive-engine.sh" archive --agent main
# 仅清理/轮转日志（读 config.yaml 中 logging.*）
bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/archive-engine.sh" log-maintenance
"$HOME/.openclaw/workspace/skills/daily-memory-archiver/bin/daily-memory-archiver" logs
```

| 参数 | 含义 |
|:---|:---|
| `--force` | 忽略阈值判断；仍受检查点与合并逻辑约束。**不**绕过 `min_new_messages` 以外的检查点（0 条新增仍会早退）；与 `--force` 组合时 **`min_new_messages` 不生效**，可立即写 memory。 |
| `--session <key>` | 仅归档该 session key（不读 `merge_jsonl_keys` 多路）。 |
| `--agent <id>` | 覆盖 `openclaw.agent_id`，选择对应 agent 的会话存储。 |

日志默认：`~/.openclaw/logs/daily-memory-archiver.log`  
可选轮转：环境变量 **`DAILY_MEMORY_LOG_MAX_BYTES`**（超过则改名为 `.1` 并新开）。

**cron 与日志（推荐）**：**不要**再单独 `>> daily-memory-archiver-cron.log`；引擎已写入 **`$OPENCLAW_HOME/logs/daily-memory-archiver.log`**（轮转见 `logging.*`）。crontab 行尾使用 **`>/dev/null 2>&1`** 即可，避免与 stderr 叠成双份、也避免 cron 发空邮件。`log()` 仅在 **stderr 为终端**时镜像一行，便于本地手跑调试。

```cron
*/30 * * * * /完整路径/.openclaw/workspace/skills/daily-memory-archiver/bin/daily-memory-archiver archive >/dev/null 2>&1
```

（若 cron 环境未设 `HOME`，请在行首 `HOME=/你的家目录` 或写死路径。）

## 4. 多通道、阈值、检查点与摘要

### 4.1 `merge_jsonl_keys`

在 **`config/config.yaml`** 的 `session.merge_jsonl_keys` 中列出要合并的完整 session key（与官方会话列表中的 key 一致）：

```yaml
session:
  key: ""
  merge_jsonl_keys:
    - "agent:main:main"
    - "agent:main:feishu:direct:<open_id>"
```

- **合并顺序**：只取 **检查点之后** 的新消息；多路存储的 user/assistant 文本经消息身份去重，再按 **`timestamp`** 排序合成一条时间线。
- **多路前缀**：合并路数大于 1 时，正文前加 **`[session_key]`**，便于区分来源。

临时覆盖（逗号分隔、键内勿加空格）：**`DAILY_MEMORY_MERGE_KEYS=key1,key2`**

**对话中追加 key**：用 `openclaw sessions --agent <agent> --limit all --json` 核对 key → `bash scripts/config-manager.sh merge-jsonl-keys-add "<key>"` → `merge-jsonl-keys-list` 查看。

### 4.2 用量与触发（`archive.trigger_mode`）

- **按 key 统计**：每个 key 从统一存储快照读取用量（优先新鲜的 `totalTokens`，否则用 `inputTokens`）；日志中会输出 **`per_key: key=用量`**。
- **是否执行本次 `archive` 主流程**（读取会话、合并、可能写 memory）由 **`trigger_mode`** 与可选的 **`archive.periodic_archive_minutes`** 共同决定：
  - **`threshold`（默认）**：至少有一个 key 的用量 **≥ `archive.threshold.max_input_tokens`** 时进入主流程；**或**（当 **`periodic_archive_minutes > 0`** 且存在 **`config/.last_archive_ts`**）距**上次成功写入 memory**已满该分钟数时也会进入主流程，以便「对话不多但重要」时仍能补归档（见 4.4）。未达阈值且未到定期间隔则退出（可用 **`archive --force`**）。
  - **`scheduled`**：每次调用都进入后续步骤（适合纯定时扫增量）。
  - **`hybrid`**：满足 **阈值**、**`check_interval_minutes`** 间隔、或 **`periodic_archive_minutes`**（若配置且 &gt;0）之一即运行。

**与 `sessions.compact` 的关系**：超阈值时照常尝试 **compact**（仍受 **`compact_only_over_threshold`**）；**仅因定期间隔**进入归档时，用量未超阈值则 **不会**对未超限 key 做 compact——与「半小时盯用量、两小时保底写 memory」一致。

**说明**：在 `threshold` 下，“是否存在 key 超阈值”与“所有 key 用量取 max 再与阈值比较”在数学上等价；差别在于后续 **compact 只打超限 key**（见下）。

### 4.3 检查点 `config/.archive_merge_checkpoint.json`

- 每个 session key 保存 **v2 消息身份游标**；下一轮按身份去重，同时间戳或延迟追加的新事件仍可归档。旧时间戳只在首次升级时作为边界，随后转换为身份记录。
- **成功写入 memory 段落**或按既定噪声策略消费后更新检查点；经验证无待写内容的空批次也可完成旧游标升级；**因冷却跳过 memory 写入**时不更新（避免“没写 md 却认为已处理”）。
- **删除该文件** 等价于清空检查点，下次会对各 key **全量**再扫一遍（历史极长时首跑成本高，且可能 **重复写入 memory**，慎用）。
- 该文件为本地状态，已在 skill **`.gitignore`** 中忽略。

### 4.4 本周期无新增 / 攒批（`archive.min_new_messages`）

- 若检查点之后 **合并结果为 0 条**消息：不写 memory，可提交已验证快照的身份游标（包括旧游标升级），**不执行 compact**。
- 若有新增但条数 **&lt; `min_new_messages`** 且 **未**使用 `--force`：不写 memory、不推进检查点、**不执行 compact**（便于攒够一批再摘要，减轻“零星一句反复摘要”）。**以下情况放宽本条**（有 ≥1 条新增即可写 memory）：**`--force`**、**用量已达阈值**、**由 `periodic_archive_minutes` / hybrid 定期间隔触发**（便于定时间隔内少量重要对话仍落盘）。

### 4.5 本地与云端摘要（`analyzer.*`）

| 配置 | 作用 |
|:---|:---|
| `messages_to_analyze` | **本地关键词提取**与**非分块时云端**默认只处理合并后 **尾部 N 条**（本周期新增流上的尾部）。 |
| `chunk_cloud_summary` | **`true`（推荐）**：若本周期新增条数 **&gt; `messages_to_analyze`**，按每段 N 条 **顺序**多次调用云端摘要，减少“只摘要尾部、前面丢失”。 |
| `max_cloud_summary_chunks` | 云端最多段数；超出时 **只摘要时间上较新的若干段**，更旧的新增可能未进 LLM，日志 **`[WARN]`**；检查点仍按 **本批全部新增**推进（若已写 memory）。 |
| `cloud_summarizer.enabled` | 是否调用云端；关闭时按 raw_detail 配置写最终原始细节，不调用云端。 |

### 4.6 冷却（`archive.threshold.cooldown_minutes`）

- 仅在 **`threshold`** 模式且 **`--force` 未开启**时生效：若与 **上次写入** 的 `usage_tokens` 相同且在冷却时间内，**跳过本次 memory 写入与 compact**，保留未归档消息。
- **由 `periodic_archive_minutes` 触发的补归档**不受冷却挡写入（避免用量几乎不变时定期间隔永远不写 md）。

### 4.7 Compact（`archive.compact`）

| 配置 | 作用 |
|:---|:---|
| `compact.max_lines` | 传给 Gateway `sessions.compact` 的 `maxLines`（默认 400）。 |
| `compact_only_over_threshold` | **`true`（默认）**：仅对用量 **≥ `max_input_tokens`** 的 key 调用 compact；**`false`** 则对每个 merge 中的 key 都调用（与 v1.3 前行为接近）。 |

`openclaw gateway call` 需 **设备配对**；遇 `pairing required` 见 **第 5 节**。

### 4.8 配置示例（与实现对齐）

```yaml
openclaw:
  agent_id: main

session:
  key: ""
  merge_jsonl_keys:
    - "agent:main:main"

archive:
  trigger_mode: "threshold"
  threshold:
    max_input_tokens: 200000
    check_interval_minutes: 5
    cooldown_minutes: 45
  min_new_messages: 1
  periodic_archive_minutes: 120   # 0=关闭；cron 可仍每 30m 调用 archive，由本项控制「至少每 2h 检查一次是否写 memory」
  compact_only_over_threshold: true
  compact:
    max_lines: 400

analyzer:
  messages_to_analyze: 50
  chunk_cloud_summary: true
  max_cloud_summary_chunks: 20
  cloud_summarizer:
    enabled: true

logging:
  log_max_bytes: 10485760
  log_keep_rotations: 5
  log_max_age_days: 30

output:
  memory_dir: "~/.openclaw/workspace/memory"

skill_version: "1.7.1"
config_version: "8"
```

### 4.9 日志轮转与清理

每次 **`archive` 开头**会执行：先按 **`log_max_age_days`** 删除过期**轮转备份**（`日志名.1`、`.2`…），再判断当前日志是否超过 **`log_max_bytes`**，若超过则链式重命名并保留 **`log_keep_rotations`** 个备份。当前活动日志不会被按天龄删除。

- **`log_max_bytes: 0`**：关闭按大小轮转。
- **`log_max_age_days: 0`**：关闭按天龄删除。

仅做日志维护、不跑归档：

```bash
bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/archive-engine.sh" log-maintenance
"$HOME/.openclaw/workspace/skills/daily-memory-archiver/bin/daily-memory-archiver" logs
```

工程结构、实现文件与维护清单见仓库根目录 **[README.md](./README.md)**。

## 5. pairing（CLI compact）

`openclaw gateway call` 需 **设备配对**。遇 `pairing required`：

```bash
export OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
export GW_TOKEN="$(jq -r '.gateway.auth.token // empty' "$OPENCLAW_HOME/openclaw.json")"
openclaw devices list --json --token "$GW_TOKEN"
openclaw devices approve --latest --token "$GW_TOKEN"
```

勿在聊天中粘贴完整 `GW_TOKEN`。cron 需 **`HOME`、`OPENCLAW_HOME`、`PATH`**（含 `openclaw`）。

## 6. `compacted: false`

Gateway 在 transcript **行数 ≤ max_lines（默认 400）** 时返回 **`compacted: false`**，属正常，表示未因超限而截断/压缩文件。

## 7. 环境变量摘要

| 变量 | 作用 |
|:---|:---|
| `OPENCLAW_HOME` | 默认 `~/.openclaw` |
| `OPENCLAW_AGENT_ID` | 覆盖 agent |
| `SESSIONS_JSON` | 旧 JSONL 索引路径，亦作为官方 CLI 的 legacy selector |
| `DAILY_MEMORY_SESSION_BACKEND` | `auto`（默认）/ `sqlite` / `jsonl` |
| `DAILY_MEMORY_SQLITE_PATH` | 显式只读 SQLite 文件路径 |
| `DAILY_MEMORY_CONFIG_DIR` | skill `config/` |
| `DAILY_MEMORY_MEMORY_DIR` | memory 输出目录 |
| `DAILY_MEMORY_LOG` | 日志路径 |
| `DAILY_MEMORY_LOG_MAX_BYTES` | 覆盖 `logging.log_max_bytes`（字节，`0` 关闭轮转） |
| `DAILY_MEMORY_LOG_KEEP_ROTATIONS` | 覆盖 `logging.log_keep_rotations` |
| `DAILY_MEMORY_LOG_MAX_AGE_DAYS` | 覆盖 `logging.log_max_age_days`（`0` 关闭按天龄删备份） |
| `DAILY_MEMORY_MERGE_KEYS` | 覆盖 merge 列表 |
| `DAILY_MEMORY_PERIODIC_ARCHIVE_MINUTES` | 覆盖 `archive.periodic_archive_minutes`（`0` 关闭定期间隔补归档） |
| `SKIP_SESSION_COMPACT` / `OPENCLAW_SKIP_COMPACT` | `1` 跳过 compact |
| `ARCHIVE_MODE` | 与向导生成 YAML 相关 |
| `MESSAGES_TO_ANALYZE` / `THRESHOLD_INPUT_TOKENS` 等 | 见 `config-manager` 模板 |

## 8. 脚本索引

| 路径 | 说明 |
|:---|:---|
| `scripts/archive-engine.sh` | `archive …`、`log-maintenance` |
| `scripts/config-manager.sh` | `init-defaults`、`save-json`、`merge-jsonl-keys-*`、`status`、`show` |
| `scripts/get-cloud-creds.sh` | JSON / `--export` |
| `scripts/extractors/local-extractor.sh` | 本地四类关键词块 |
| `scripts/summarizers/cloud-summarizer.sh` | OpenAI 兼容 Chat Completions |
| `scripts/lib/credentials-store.sh` | 加解密 |
| `scripts/lib/log-maintenance.sh` | 日志天龄清理与链式轮转 |
| `scripts/self-check.sh` | 自检 |
| `bin/daily-memory-archiver` | `archive`、`logs` 等快捷入口 |
| `README.md` | 维护者：目录、配置、扩展清单 |

## 9. 从全局 `skills` 迁到工作区 `workspace/skills`（迁移清单）

适用于当前 skill 在 **`$HOME/.openclaw/skills/daily-memory-archiver`**、希望改到 **`$HOME/.openclaw/workspace/skills/daily-memory-archiver`** 的情况（与 §0.1 一致）。

1. **暂停 cron**：注释 crontab 里调用 `daily-memory-archiver` 的行，避免搬迁过程中双实例或路径错误。
2. **建目录**：`mkdir -p "$HOME/.openclaw/workspace/skills"`
3. **搬迁（二选一）**  
   - **整目录 `mv`（推荐，保留 Git 与 config）**：  
     `mv "$HOME/.openclaw/skills/daily-memory-archiver" "$HOME/.openclaw/workspace/skills/"`  
     若目标已存在同名目录，先 `mv` 走或改名备份，勿盲目覆盖。  
   - **新 clone + 拷 config**：在新路径 `git clone …` 后，将旧目录下 **`config/`**（含 `config.yaml`、`credentials.enc`、`.master_key`、`.archive_merge_checkpoint.json`、`.last_archive_ts` 等运行时文件）拷入新仓库的 `config/`，权限保持 **`chmod 700 config`**。
4. **改 crontab**：把可执行文件路径改为  
   `"$HOME/.openclaw/workspace/skills/daily-memory-archiver/bin/daily-memory-archiver"`（或写绝对路径）。
5. **OpenClaw / Cursor**：确认助手或 Control UI 从**新路径**加载 `SKILL.md`（视你使用的 OpenClaw 版本对工作区 `skills/` 的扫描规则而定）。
6. **验证**：  
   `bash "$HOME/.openclaw/workspace/skills/daily-memory-archiver/scripts/self-check.sh"`  
   再按需执行 `archive`（生产环境慎用 `--force`）。
7. **清理**：确认无其他脚本引用旧路径后，删除 **`$HOME/.openclaw/skills/daily-memory-archiver`** 下残留（若已 `mv` 则旧路径应已不存在）。

**过渡期**：若仍有工具写死旧路径，可短期  
`ln -s "$HOME/.openclaw/workspace/skills/daily-memory-archiver" "$HOME/.openclaw/skills/daily-memory-archiver"`  
长期仍应改为新路径或环境变量。

## 10. 重载 Skill

修改 `SKILL.md` 后请 **新开会话** 或 `openclaw skills check`，并用 **read** 重新加载本文。

---

*Made for OpenClaw.*
