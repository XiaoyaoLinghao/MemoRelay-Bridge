# 1.7.0 本地验证记录

基线：`5a9c363cdf96b38ba2a8402c2848fc93babbbe0d`。
环境：Windows，MSYS2 Bash，Python 3.12.10，jq 1.8.1。
数据：测试临时创建的 JSONL / schema 19 SQLite fixtures；未读取真实用户
会话，未调用真实云端摘要或真实 compact。

## 最终结果

| 检查 | 结果 |
| --- | --- |
| `python -m unittest -q tests/test_session_store.py` | 15 项通过 |
| `bash tests/test-session-store-integration.sh` | 全部通过，含交互接口及空批次迁移 |
| `bash scripts/self-check.sh` | 退出码 0，全部通过 |
| 既有 v1.6.1 / v1.6.2 / v1.6.3 修复检查、DMA fix-plan | 通过 |
| 版本 / config / spec 镜像、文档链接、`git diff --check` | 通过 |
| `tests/test-regression.sh` | 仅 R3e 的 Windows/MSYS 文件名问题未通过，见下文 |
| 独立完整差异审查 | 本地候选通过，无未关闭的已验证 P1/P2 产品问题 |

设计中的四个场景均已通过本地 fixture 验证：旧存储重复运行、SQLite-only
归档、身份游标迁移续档、错误不提交状态。实际部署验收仍在文末列为待办。

## 验证范围

- 统一读取器的公共子进程 JSON 接口，JSONL 与 SQLite 元数据及长文本。
- 身份增量、同时间戳追加、旧检查点升级、正式迁移保持 session/event ID
  时的去重、多窗口历史，以及未选中 key 的检查点保留。
- 未知 schema、缺失/损坏所选会话、会话身份冲突、错误发现响应。
- SQLite 一致读事务、并发未提交 WAL 写入、主数据库内容保持不变。
  SQLite 创建/更新 WAL/SHM 读者协调文件不等同于修改记录。
- 实际 shell 归档入口的原文输出、再次运行不重复、迁移续档、错误不提交、
  延期不 compact、摘要失败不推进检查点，以及交互入口的错误传播。
- 既有格式、提取器、噪声和版本修复回归。

## 已分类的基线/环境问题

`scripts/test-fail-guard.sh` 在未改动的上游基线也失败：它要求源码中
`cloud_recoverable_fail=1` 至少出现 4 次，实际代码已合并为 3 个赋值分支。
本候选将这个计数断言替换为真实归档入口的失败/延期行为检查，保留其他
既有检查。判定为 REPORT（测试断言与当前代码结构不符），不是新增产品故障。

`tests/test-regression.sh` 的 R3e 在本环境失败：测试创建的 sidecar 文件名
含 `HH:MM` 冒号，MSYS 与原生 Windows jq 对该路径的处理不一致。
其余检查通过。该测试和源码原行为未为此放宽；判定为 ENVIRONMENT，
完整 Linux 路径行为留待目标平台验证。

端到端测试另定位到既有 PRODUCT 问题：配置加载器把嵌套
`cloud_summarizer.enabled` 当成点分键名搜索，导致 `enabled: false` 被忽略。
已按实际配置的缩进层级读取该开关，并保留旧点分写法作为回退。

自检组合还修正了 TEST INFRASTRUCTURE 问题：Wave10 测试覆盖并删除继承的
`TMPDIR`，使后续测试无法创建临时目录。已使用独立测试目录变量；完整集成
检查由 self-check 执行一次，Wave10 仅复用结构检查，避免重复执行同一套。

## 独立审查

审查覆盖从基线到候选的全部代码及新文件，重点包含存储读取、消息身份、
检查点提交和交互接口。审查推动关闭：交互列表仍依赖旧文件、交互归档
吞掉错误、空批次未完成旧游标升级、缺失当前窗口被当作空记录等问题。
曾提出的两个假设经官方代码核对撤回：正式迁移会保持 session ID；
CLI 的 `sessions.json` 参数是逻辑 selector，检查的是解析后的 SQLite 文件。

## 尚未证明的边界

- 真实 OpenClaw CLI/用户迁移数据库、Linux 主机和实际云端摘要链路。
- Python 3.9 最小版本运行（本机验证为 3.12）。
- 源数据已有丢失、旧时间戳检查点无法表达的历史遗漏。
- 断电发生在 Markdown 写入与检查点替换之间的跨文件原子性。
- 尚无上游 push、PR、tag 或 release；本地候选不代表上游已适配。
