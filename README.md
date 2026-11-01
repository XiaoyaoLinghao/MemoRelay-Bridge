# MemoRelay-Bridge · 忆桥

忆桥：宿主适配、只读采集、归一化、秘密保护、可靠outbox/投递及召回接入。语义提炼由Core统一负责。

属于 [承忆 · MemoRelay](https://github.com/XiaoyaoLinghao/MemoRelay)。版本：**2.0.0rc1，试运行候选**。

## 安装与使用

    python3.11 -m pip install .
    memorelay-bridge --help

Python >=3.11，核心无第三方运行依赖，面向macOS13/Intel/16GB。本仓库是从当前已审查部署基线导出的完整源码快照；不是已完成长期生产验收的正式版。原仓库 daily-memory-archiver 的历史版本保持独立。

## 兼容性

新Python distribution为 memorelay-bridge；新命令为 memorelay-bridge。旧的Python模块 dma、旧CLI别名、数据库格式、契约ID、原生宿主插件入口和旧环境变量保留兼容，不因产品命名改变而强制迁移数据。安装时不要与同模块旧distribution混装；使用新venv。此发布不会自动更换正在试运行的宿主安装。

可选 redaction-yaml extra 提供锁定的PyYAML6.0.3，用于显式YAML凭据引用。适配器不另行生成语义事实。

## 来源与开发

原始提交：94312484d0c199372d8be567ba7c5b7d560d7183。见 [SOURCE_PROVENANCE.json](SOURCE_PROVENANCE.json)，其记录原始文件哈希及本次品牌/打包改动。src业务实现、契约、迁移与回归保留；新增入口使用原实现。

[改名前README](docs/legacy/README-before-MemoRelay.md)含旧1.x操作方式，作为历史参考；当前2.x入口以本文和总仓库说明为准。docs/、contracts/与tests/随代码保留。旧CLAUDE/SKILL中的v1操作不能自动用于v2。

测试需在显式隔离目录/数据库执行，不读真实默认HOME、不调用真实模型或钥匙串。主项目包含跨仓库E2E与操作脚本。此次只为重新命名及发布；长期观察和原54项剩余验收仍未完成。

## 使用范围

源码可见性由 GitHub 仓库设置决定；许可证尚未另行指定，保留原版权与上游记录。
