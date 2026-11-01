# Daily Memory Archiver

## Runtime status for KW health monitoring

The additive [DMA runtime status v1 contract](docs/DMA_RUNTIME_STATUS_V1.md) supplies
operational evidence without changing Memory SPEC v1.1, archive selection, summarization,
noise filtering or compaction policy. The archive engine publishes an atomic JSON record
at `DAILY_MEMORY_STATUS_PATH`, defaulting to `config/.runtime_status.json` under the
resolved `DAILY_MEMORY_CONFIG_DIR` (or this installation's config directory).

The record distinguishes running, archived, idle, noise-only, deferred, failed and
partial runs. Pending messages are observations from that run's validated snapshot,
not a continuously updated queue length. Per-operation failures are reset only by
success of the same operation; idle/noise-only runs cannot prove summary recovery.
`last_archived_at` excludes noise-only and partial output. Checkpoint progress is based
on content changes, not mtime. Status evidence errors remain visible to the consumer.

Status publication requires the existing `flock` archive lock. Without `flock`, DMA
retains its previous archive behavior, emits a monitoring-unavailable diagnostic and
does not publish an unsafe concurrent status record. No replacement lock is introduced.

Deploy `scripts/archive-engine.sh`, `scripts/lib/runtime-status.sh` and
`scripts/runtime-status.py` together from the same reviewed revision. KW requires its
matching status reader and report wrapper; see the contract for rollout and validation.
The existing `health-check` command is a separate limited Memory/retry diagnostic and
does not replace the runtime record. Status contains no raw conversations or credentials.

This change does not migrate the session or knowledge database, replay historical Memory,
or deploy itself. Start development from the SQLite-capable v1.7.1 baseline or newer;
do not overwrite an existing v1.7.1 installation with a stale development checkout.

OpenClaw 会话归档 Skill：原生读取 OpenClaw SQLite 或旧版 JSONL 会话，多 session 按时间合并、检查点增量、本地关键词提取、可选云端 LLM 摘要、按 key 用量触发与选择性 `sessions.compact`。

[最新正式版：v1.7.1](https://github.com/XiaoyaoLinghao/daily-memory-archiver/releases/tag/v1.7.1)

**面向维护者**：本文描述仓库布局、配置键、入口脚本与扩展点。面向 Cursor / 助手的交互说明见根目录 **[SKILL.md](./SKILL.md)**（安装、配对、cron、**全局 skills vs workspace/skills**、迁移清单等）。

**安装位置**：自维护 / Git 管理推荐 **`$HOME/.openclaw/workspace/skills/daily-memory-archiver`**；全局目录 **`$HOME/.openclaw/skills/daily-memory-archiver`** 亦支持。脚本以自身路径解析 `SKILL_ROOT`，与安装在哪一侧无关。

## 版本与兼容

- **当前正式版：1.7.1**（`config_version: "8"`，实现 `KW_MEMORY_FILE_SPEC` v1.1）：在 SQLite 原生读取与跨存储增量检查点基础上，增加 DMA 运行状态观测，区分已恢复、空闲、噪声、待处理与失败，避免将历史错误和无新增 Memory 文件误报为当前故障。
- 实现与 **SKILL.md** 中 `skill_version` / `config.yaml` 中 `config_version` 应对齐；以脚本行为为准。
- config schema 未变（lexicon 为 env 变量，非 config.yaml 键），故 `config_version` 保持 `8`。
- Shell：**bash 4+**（使用关联数组 `declare -A`），Python **3.9+**（含标准库 `sqlite3`）、jq。
- 安装、迁移、离线路径与回退步骤见 **[SQLite 升级说明](docs/sqlite-upgrade.md)**。

## 安装与升级

新安装建议将正式版放在 OpenClaw 工作区 skills 目录：

```bash
git clone --branch v1.7.1 --depth 1 \
  https://github.com/XiaoyaoLinghao/daily-memory-archiver.git \
  "$HOME/.openclaw/workspace/skills/daily-memory-archiver"
cd "$HOME/.openclaw/workspace/skills/daily-memory-archiver"
bash scripts/self-check.sh
```

从 1.6.x 升级时，先暂停 DMA 定时任务并等待当前归档结束，备份整个 `config/`（包括隐藏的检查点文件）和 memory 目录，再更新代码。不要用空配置覆盖原有配置。完整迁移及回退边界见 **[SQLite 升级说明](docs/sqlite-upgrade.md)**。

升级后先核对 agent 和完整 session key：

```bash
openclaw sessions --agent main --limit all --json
```

将实际 key 写入 `config/config.yaml` 的 `session.merge_jsonl_keys`。首次归档建议先禁用 compact，并只验证一个实际存在的会话：

```bash
SKIP_SESSION_COMPACT=1 \
  bash scripts/archive-engine.sh archive --force \
  --agent main --session 'agent:main:main'
```

再次执行同一命令应不重复写入。确认输出、日志及 `config/.archive_merge_checkpoint.json` 后，再恢复正式调度和 compact。生产数据验收仍应在实际 OpenClaw 主机完成。

默认 `DAILY_MEMORY_SESSION_BACKEND=auto`：优先使用 OpenClaw 发现到的 SQLite，旧 `sessions.json` + JSONL 继续作为兼容路径。SQLite 模式使用只读连接并检查受支持的 schema；当前支持 OpenClaw agent schema 19，未知版本会明确失败。

## 仓库布局

| 路径 | 说明 |
|:---|:---|
| `SKILL.md` | Cursor Skill 元数据与用户向文档（必读入口） |
| `README.md` | 本文件：工程与维护说明 |
| `config/config.yaml` | 主配置（无密钥） |
| `config/credentials.enc` | 云端 API 加密凭证（勿提交） |
| `config/.master_key` | 解密用（勿提交） |
| `config/.archive_merge_checkpoint.json` | 各 key 已消费消息身份游标（兼容旧时间戳的首次升级）（运行时生成，勿提交） |
| `bin/daily-memory-archiver` | CLI 包装：`init`、`archive`、`logs`、`check`、`status`、`creds` |
| `scripts/session-store.py` | 统一 JSONL / 只读 SQLite 快照与增量消息读取 |
| `scripts/archive-engine.sh` | 核心：`archive`、`log-maintenance` |
| `scripts/config-manager.sh` | `init-defaults`、`save-json`、merge keys、`status`/`show` |
| `scripts/get-cloud-creds.sh` | 解密并输出 JSON（api_url / api_token / model） |
| `scripts/lib/log-maintenance.sh` | 日志按天龄删除轮转备份、按大小链式轮转 |
| `scripts/lib/credentials-store.sh` | OpenSSL 加解密 |
| `scripts/lib/conversation-noise.sh` | 本地提取噪声过滤 |
| `scripts/extractors/local-extractor.sh` | stdin JSON 消息 → Markdown 块 |
| `scripts/summarizers/cloud-summarizer.sh` | OpenAI 兼容 Chat Completions |
| `scripts/self-check.sh` | 依赖与 `bash -n` |

## 主流程（`archive-engine.sh archive`）

1. `load_config`：读 `config.yaml` + 环境变量覆盖。
2. 解析 `merge_jsonl_keys` / `DAILY_MEMORY_MERGE_KEYS`，经统一读取模块取得 SQLite 或旧 JSONL 快照；发现或校验失败立即停止。
3. 快照按消息身份去重、跨 key 排序；随后运行待补归档、空日标记和日志维护。
4. 阈值 / hybrid / scheduled 与可选 **`periodic_archive_minutes`** 判断是否继续。
5. 读取快照中的本批消息；无新增时可提交身份游标升级，不写 memory、不 compact。
6. `min_new_messages`、空新增早退；**超阈值或定期间隔触发**时可放宽 `min_new`；冷却可跳过 memory 写入（定期间隔触发时不受冷却挡写入）。
7. 本地提取、`cloud-summarizer`（可分块）、追加 `memory/YYYY-MM-DD.md`，更新检查点与 meta。
8. `sessions.compact`（可选仅超限 key）。

## 日志清理与轮转

默认日志路径：`$OPENCLAW_HOME/logs/daily-memory-archiver.log`（可用 `DAILY_MEMORY_LOG` 覆盖）。**cron**：行尾 **`>/dev/null 2>&1`**，**勿**单独 `>>…-cron.log`（见 **SKILL.md** 日志与 crontab 示例）。`log()` 仅在 stderr 为终端时镜像一行。

| 配置项（`config.yaml` → `logging`） | 环境变量（优先于 YAML） | 含义 |
|:---|:---|:---|
| `log_max_bytes` | `DAILY_MEMORY_LOG_MAX_BYTES` | 当前日志超过该字节则轮转；**`0` 表示不按大小轮转** |
| `log_keep_rotations` | `DAILY_MEMORY_LOG_KEEP_ROTATIONS` | 保留备份个数 `.1` … `.N`（默认 5） |
| `log_max_age_days` | `DAILY_MEMORY_LOG_MAX_AGE_DAYS` | 删除**轮转备份** `$(basename).*` 中**早于**该天数的文件；**`0` 表示不按天龄删除**（不删当前活动日志） |

行为顺序：先按天龄删旧备份，再判断当前文件是否超 `log_max_bytes` 并链式重命名。

**单独执行维护**（不跑归档）：

```bash
bash scripts/archive-engine.sh log-maintenance
# 或
./bin/daily-memory-archiver logs
```

可与 cron 低频搭配（例如每日一次），与每次 `archive` 内嵌维护二选一或并存均可。

实现文件：`scripts/lib/log-maintenance.sh`。

## 配置速查（扁平 `yaml_scalar`）

脚本用 `grep '^[[:space:]]*key:'` 式读取，键名在全文应唯一。常用键：

- `openclaw`：`agent_id`
- `session`：`key`、`merge_jsonl_keys`（列表由 awk 解析）
- `archive`：`trigger_mode`、`max_input_tokens`、`check_interval_minutes`、`cooldown_minutes`、`min_new_messages`、`periodic_archive_minutes`（定期间隔补写 memory，与 compact 解耦）、`compact_only_over_threshold`、`max_lines`（compact）
- `analyzer`：`messages_to_analyze`、`chunk_cloud_summary`、`max_cloud_summary_chunks`、`cloud_summarizer.enabled`
- `logging`：`log_max_bytes`、`log_keep_rotations`、`log_max_age_days`
- `output`：`memory_dir`

## 环境变量（摘录）

| 变量 | 作用 |
|:---|:---|
| `OPENCLAW_HOME` | 默认 `~/.openclaw` |
| `OPENCLAW_AGENT_ID` | 覆盖要读取的 agent ID |
| `DAILY_MEMORY_SESSION_BACKEND` | `auto`（默认）/ `sqlite` / `jsonl` |
| `DAILY_MEMORY_SQLITE_PATH` | 显式指定只读 SQLite 文件，供离线或自定义路径使用 |
| `SESSIONS_JSON` | 旧 JSONL 索引路径，亦作为官方 CLI 的 legacy selector |
| `DAILY_MEMORY_CONFIG_DIR` | skill `config/` |
| `DAILY_MEMORY_LOG` | 日志文件路径 |
| `DAILY_MEMORY_LOG_MAX_BYTES` 等 | 见上表 |
| `DAILY_MEMORY_MERGE_KEYS` | 逗号分隔覆盖 merge 列表 |
| `DAILY_MEMORY_PERIODIC_ARCHIVE_MINUTES` | 覆盖定期间隔补归档（分钟，`0` 关闭） |
| `SKIP_SESSION_COMPACT` / `OPENCLAW_SKIP_COMPACT` | `1` 跳过 compact |

完整列表见 **SKILL.md** 第 7 节。

## 修改代码时的检查清单

1. `bash scripts/self-check.sh` 或 `bash -n` 相关脚本。
2. 新增 `config.yaml` 键：在 `load_config` 中读取，在 `config-manager.sh` 的 `save_config_yaml` 中写入默认值（若适用），并更新 **SKILL.md** / **README.md**。
3. 变更归档语义时同步 **SKILL.md**（用户向）与本 **README**（维护向）。
4. 密钥与运行时文件保持在 **`.gitignore`** 中。

## 许可证与上游

本仓库为 OpenClaw 生态下的 Skill；具体许可证以仓库根目录为准（若单独声明）。
