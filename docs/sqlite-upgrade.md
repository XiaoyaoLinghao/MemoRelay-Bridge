# DMA 1.7.0 SQLite 升级说明

这是本地候选版本，尚未发布到上游，也未在实际 OpenClaw 主机上验收。
`skill_version` 以根目录 `SKILL.md` frontmatter 为准；配置 schema 保持 8，
输出 `KW_MEMORY_FILE_SPEC` 保持 1.1。

## 支持边界

- Bash 4+、jq、Python 3.9+（标准库 sqlite3）；沿用现有 curl/OpenSSL 依赖。
- OpenClaw agent 数据库 schema 19，包含 `session_nodes`、`session_windows`、
  `transcript_events`；未知版本明确报错，不猜测表结构。
  检查的是版本、归属和读取所需结构，不替代 OpenClaw doctor 的完整数据库
  完整性诊断；事件身份以原始 `event_json.id` 为准，不读取派生身份索引。
- 旧 `sessions.json` + JSONL 仍可读取。适配器直接读 SQLite 原始事件，
  不使用会脱敏或截断消息的 `sessions_history`，也不生成旧格式兼容文件。
- “完整”指读取器保留 user/assistant 的文本内容，不截断长文本；原有噪声
  过滤、摘要条数上限、原始细节配置仍生效。图片、音频及工具事件不因此
  成为逐字归档内容，DMA 也不替代数据库备份。

## 在实际主机升级

1. 暂停 DMA 的定时任务，等待正在运行的归档完成。
2. 备份现有 DMA 代码、整个 `config/`（含隐藏状态文件）、memory 目录；
   凭据备份应保留原访问权限。不要删除或修复 OpenClaw 的源数据库。
3. 将本候选代码安装到原 DMA 目录，保留原配置、凭据及检查点。
4. 执行 `bash scripts/self-check.sh`。确认 `python3` 导入 sqlite3 成功。
5. 用 `openclaw sessions --agent main --limit all --json` 核对完整 key 和
   返回的物理数据库路径；按实际 agent 修改 `main`。
6. 首次验证在隔离的配置副本和输出目录运行，设置
   `DAILY_MEMORY_CONFIG_DIR`、`DAILY_MEMORY_MEMORY_DIR`、`DAILY_MEMORY_LOG`；
   关闭副本中的云端摘要并设置 `SKIP_SESSION_COMPACT=1`，然后运行
   `bash scripts/archive-engine.sh archive --force --agent main --session <key>`。
   检查输出、日志和检查点；再运行一次，确认不重复写入。
7. 在隔离验证通过后恢复正式配置和调度；实际数据迁移比对及云端链路
   需要在目标主机另行验收。

## 存储选择

| 环境变量 | 含义 |
| --- | --- |
| `DAILY_MEMORY_SESSION_BACKEND=auto` | 默认，优先发现 SQLite，旧文件作为兼容路径 |
| `DAILY_MEMORY_SESSION_BACKEND=sqlite` | 要求 SQLite，错误时不回退旧文件 |
| `DAILY_MEMORY_SESSION_BACKEND=jsonl` | 显式读取旧索引和 JSONL |
| `DAILY_MEMORY_SQLITE_PATH=/absolute/path/openclaw-agent.sqlite` | 指定 SQLite 文件，供离线或自定义路径使用 |
| `SESSIONS_JSON=/absolute/path/sessions.json` | 旧索引路径 / 官方 CLI legacy selector |

常规发现使用官方 `openclaw sessions --agent ID --store SELECTOR --limit all
--json`。SQLite 原始文本通过只读连接和读事务取得。所选会话读取失败时
整批失败，不以“跳过该会话”继续提交其他检查点。
只读保证针对数据库记录、schema 和主数据库内容；SQLite 自身可能创建或
更新 WAL/SHM 临时协调文件。这里不承诺源目录逐字节不变，也不会用
`immutable=1` 绕过活跃数据库的 WAL 和锁。如果目录权限不允许 SQLite
完成必要的读取协调，会明确失败。
因此需要先清理配置中已失效的 key；仓库示例中的多通道 key 不能直接
当作你主机上的真实会话。可用 `--session` 单独验证一个实际存在的 key。

本次也修复了“未达到最少新增消息数时仍尝试 compact”的路径。延迟归档
和无新增消息的检查均不再修改源会话，避免未消费内容被压缩。

## 增量迁移和回退

新游标仍保存在 `config/.archive_merge_checkpoint.json`，升级为每 key 的
消息身份记录。JSONL 与 SQLite 中保持相同会话/事件身份的内容不会因存储
切换重复归档；同一时间戳下的新事件仍能被识别。

旧版仅保存最后时间戳。首次升级把该时间及之前的消息视为已消费；旧版
是否漏掉后来插入的同时间戳消息无法单凭旧检查点判断。需要严格核对历史
完整性时，应与备份/原始记录比对，不应宣称自动修复了既有漏记。

身份记录会随历史增长，不能随意清空。回退到 1.6.x 时须恢复升级前的
代码和检查点备份，并处理升级后新增的 memory 内容，防止重复归档。
仅替换代码而保留新游标不受支持。若 OpenClaw 已迁移为 SQLite-only，
旧 DMA 本身仍无法工作，回退 DMA 不等于回退 OpenClaw 存储。

当前每次检查都会读取所选 key 的保留历史，再判断是否触发归档；“增量”
指输出去重，不代表数据库只读取新增行。低用量轮询也承担历史扫描成本，
大库性能尚待实际主机验证，配置时应限定需要归档的 key。

文件写入与检查点是两个步骤：断电或进程在两步间被杀仍可能重放一批，
本版本不承诺崩溃场景下的跨文件原子提交。

## 上游依据

- [OpenClaw sessions CLI](https://docs.openclaw.ai/cli/sessions)
- [Session tools 的历史读取限制](https://docs.openclaw.ai/concepts/session-tool)
- [官方数据库说明](https://docs.openclaw.ai/reference/database-schemas)
- 对照 OpenClaw `v2026.9.2` commit
  `87d32a44ab9744903d36a33b399c26cfc2b078d6` 的
  `src/state/openclaw-agent-schema.sql` 和 `openclaw-agent-db-contract.ts`。

本地 fixture 测试结果及尚未执行的目标环境验收见 [验证记录](sqlite-validation.md)。
