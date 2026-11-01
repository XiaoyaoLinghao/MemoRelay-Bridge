#!/usr/bin/env bash
# 用法: cloud-summarizer.sh --file <plain.txt> openai-compatible <model> <base_url> <api_key>
# 输出: 为 OpenClaw Dream 模式优化的结构化 Markdown
set -euo pipefail
[ "${1:-}" = "--file" ] && shift && file="$1" && shift || exit 2
kind="$1" model="$2" base="$3" key="$4"
[ -f "$file" ] || exit 1

# 去掉 NUL，限制长度，避免 shell/jq 处理异常
body_tmp=$(mktemp)
payload_tmp=$(mktemp)
resp_tmp=$(mktemp)
chmod 600 "$body_tmp" "$payload_tmp" "$resp_tmp"
trap 'rm -f "$body_tmp" "$payload_tmp" "$resp_tmp"' EXIT
tr -d '\000' <"$file" | head -c 120000 >"$body_tmp"

# ============================================================================
# v1.2: 三部分 摘要 Prompt — 叙事段落 + [关键X] tag 行 + ### 结构化事实 JSON 块
#   前两部分供 OpenClaw 召回；第三部分(### 结构化事实)供 KW 结构化摄入(W2,替代正则)。
#   可选: 注入 DAILY_MEMORY_LEXICON(KW registry --lexicon)让 name 输出规范名(B 方案)。
# ============================================================================
sys='你是 Daily Memory Archiver 的对话归纳助手。请用中文输出以下严格结构化的摘要内容，供 Knowledge Weaver 抽取知识实体与 OpenClaw 长期记忆召回使用。

输出格式分三部分：

第一部分（必须）：1-3 个段落的叙事性摘要，描述本次对话的核心进展与重点
  * 每段保留具体细节（用户原话引用 / 时间节点 / 项目名 / 技术栈）
  * 总长度不超过 400 字
  * 不要使用任何 ** 粗体标记
  * 不要使用任何 - bullet 列表

第二部分（仅当本时段有实质知识内容时输出）：用以下 9 种 tag 标注关键实体，每条一行：

  [关键决策] <内容>     - 明确的决定、选择、结论
  [关键偏好] <内容>     - 用户明确的偏好、习惯、风格
  [关键事实] <内容>     - 重要事实、状态、当前情况
  [关键风险] <内容>     - 潜在问题、需留意的风险
  [关键技术] <内容>     - 项目进展、技术方案、工具选择
  [已完成] <内容>       - 已完成的工作、实现、修复
  [待办] <内容>         - 后续要做的具体事项
  [创意] <内容>         - 新想法、灵感、可能性
  [关键讨论] <内容>     - 重要讨论、疑问、问题点

规则：
1. tag 必须置于行首，与内容用单个空格分隔
2. 一行只能有一个 tag
3. tag 后的内容应包含具体上下文（用户原话引用 / 在 X 场景下 / 因为 Y 理由），便于事后召回原始语境
4. 没有相应内容的 tag 不要输出——不需要写"暂无"或"- 无"
5. 同类内容可以多条（例如 3 条 [关键决策] 都不重复时分别列出）
6. 不要输出任何其它 ** 粗体标记或 - bullet 列表
7. 不要输出"对话归纳"等元描述
	8. ⚠️ 无实质内容判定：如果本时段对话【只包含】以下任意情况，则【不要输出任何 tag】，第一部分叙事只写一句话说明"本时段无实质内容（仅系统运维/心跳）"，并在最末尾单独输出一行机器标记 [空时段]（逐字输出此 5 字符标记，供下游程序稳定判定空时段；有实质内容时【绝不要】输出它）：
	   * 仅有 OpenClaw 心跳轮询（[OpenClaw heartbeat poll] / heartbeat）
	   * 仅有系统状态检查（磁盘空间、Docker 容器、内存等运维巡检）
	   * 仅有每日 DAILY 检查、pending 子会话检查、stale 残留检查
	   * 仅有 cron 调度、定时任务的执行记录
	   * 用户未提出任何实质需求、决策、问题或讨论
	9. ⚠️ 禁止把"本次对话仅包含X""无实质内容""系统状态正常"这类【元描述】写成 tag。元描述不是知识，宁可不输出 tag，也不要写元描述 tag。
	10. ⚠️ 禁止把运维巡检的瞬时状态（如"磁盘使用 37%""剩余 60G""Docker 状态正常"）写成 [关键事实]。这些是运维快照，不是长期知识。

⚠️ 不属于知识、不应归入任何 tag 的内容：
  * 会话操作日志（如"上线""登录""执行命令"等系统事件）
  * 工具安装/卸载/更新的过程记录
  * cron 部署、时区切换的操作步骤
  * 这些属于系统运维日志，应直接忽略
  * OpenClaw 心跳轮询、heartbeat poll
  * 磁盘/内存/Docker 容器状态巡检
  * 每日 DAILY 检查、pending 子会话检查、stale 会话残留检查

第三部分（仅当本时段有实质知识内容时输出）：在最末尾追加一个【结构化事实】区块，供 Knowledge Weaver 直接结构化摄入（替代正则抽取）。严格按以下格式：

### 结构化事实

```json
[
  {"type": "<类型>", "name": "<规范名>", "summary": "<一句话>", "aliases": ["<别名>"]}
]
```

结构化事实规则：
1. type 只能取这 8 种之一：project / decision / preference / fact / risk / task / tech / idea
   （project=项目，decision=决策/结论，preference=用户偏好/习惯，fact=重要事实/状态，risk=风险，task=待办或已完成的事项，tech=技术/工具/方案，idea=创意/想法）
2. name 用规范名——同一事物始终用同一个名字。若下方提供了【已知项目规范名】，必须优先采用其规范名，并把对话里出现的其它写法（中英、简称等）放进该条的 aliases。
3. 同一概念只输出一条，且 type 必须前后一致（例如不要把“家庭大脑”一会儿当 project、一会儿当 fact）。
4. summary 一句话（≤60 字）；aliases 可省略或为空数组。
5. 必须是合法 JSON 数组，放在 ```json 围栏内，且紧跟在 “### 结构化事实” 标题之后。
6. 无实质内容时段（仅心跳/运维）：不要输出 “### 结构化事实” 区块（与 tag 规则一致）。

示例输出：

今天用户在 ExampleProject 项目推进了 Python FastAPI 后端选型，完成了登录模块基础架构与单元测试。用户在 09:30 提到"决定用 FastAPI，因为 vim 配 jedi 很顺"，表达了对简洁代码风格的偏好。讨论中发现 PostgreSQL 17 在高并发下连接池可能耗尽，需要后续优化。

[关键决策] 后端框架：Python FastAPI（用户 09:30 原话："决定用 FastAPI，因为 vim 配 jedi 很顺"）
[关键偏好] 编辑器：vim 配 jedi（用户提到集成顺手）
[关键偏好] 代码风格：简洁直接，避免过度抽象
[关键事实] ExampleProject 运行环境：macOS 14.5 + Python 3.11 + PostgreSQL 17
[关键风险] DB 连接池配置过小在高并发下可能耗尽（需评估池大小）
[已完成] 完成 ExampleProject 登录模块基础架构与单元测试
[待办] 接入第三方 OAuth2 集成（本周内）

### 结构化事实

```json
[
  {"type": "project", "name": "ExampleProject", "summary": "推进 FastAPI 后端选型与登录模块"},
  {"type": "decision", "name": "后端用 FastAPI", "summary": "选 FastAPI 作后端框架"},
  {"type": "preference", "name": "简洁代码风格", "summary": "偏好简洁直接、避免过度抽象"},
  {"type": "tech", "name": "PostgreSQL 17", "summary": "运行环境数据库"},
  {"type": "risk", "name": "DB 连接池耗尽", "summary": "高并发下连接池可能耗尽，需评估池大小"},
  {"type": "task", "name": "接入 OAuth2", "summary": "本周内接入第三方 OAuth2"}
]
```

无实质内容时段的正确输出示例（仅心跳，不输出任何 tag）：

本时段无实质内容（仅系统心跳轮询与每日状态检查，无用户实质输入）。'

# B 方案：若调用方注入了项目词表（DAILY_MEMORY_LEXICON，来自 KW `registry --lexicon`），
# 追加到 system prompt，让 LLM 的结构化事实 name 直接输出规范名（预防名字分裂）。
if [ -n "${DAILY_MEMORY_LEXICON:-}" ]; then
    sys="${sys}

【已知项目规范名（结构化事实的 name 必须优先采用这些规范名，其它写法放入该条 aliases）】：
${DAILY_MEMORY_LEXICON}"
fi

base="${base%/}"
case "$base" in
    */chat/completions) url="$base" ;;
    *) url="${base}/chat/completions" ;;
esac

# --rawfile 避免对话中的引号/反斜杠破坏 jq（需 jq ≥1.5）
# payload 写临时文件，避免 -d "$payload" 超过 ARG_MAX 导致 curl 失败
if ! jq -n \
    --arg m "$model" \
    --arg s "$sys" \
    --rawfile b "$body_tmp" \
    '{model:$m, temperature: 0.3, messages:[{role:"system",content:$s},{role:"user",content:("请归纳以下对话内容，按系统提示中的三部分格式输出：叙事摘要 + 关键 tag + 结构化事实区块：\n\n" + $b)}]}' \
    >"$payload_tmp" 2>/dev/null; then
    echo "- *（构建请求 JSON 失败；检查 jq 版本与输入内容）*" >&2
    exit 1
fi

# response 写临时文件（-o），避免 resp=$(curl) 管道写入失败触发 curl: (23)
if ! curl -sS --max-time 120 -X POST "$url" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $key" \
    -d @"$payload_tmp" \
    -o "$resp_tmp"; then
    echo "- *（curl 请求失败）*" >&2
    exit 1
fi

if ! jq empty "$resp_tmp" 2>/dev/null; then
    echo "- *（API 返回非 JSON；可能被网关/HTML 拦截，见日志前 500 字）*" >&2
    head -c 500 "$resp_tmp" >>"${DAILY_MEMORY_LOG:-/dev/null}" 2>/dev/null || true
    exit 1
fi

if jq -e '.error' "$resp_tmp" >/dev/null 2>&1; then
    echo "- *（API 错误: $(jq -r '(.error.message? // .error) | tostring' "$resp_tmp")）*" >&2
    exit 1
fi

out=$(jq -r '.choices[0].message.content // empty' "$resp_tmp")
if [ -z "$out" ]; then
    echo "- *（摘要响应为空）*"
    exit 1
fi
echo "$out"
