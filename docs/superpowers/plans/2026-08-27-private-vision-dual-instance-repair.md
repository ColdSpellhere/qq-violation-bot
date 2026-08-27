# Private Vision and Dual-Instance Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task.

**Goal:** 让白名单私聊安全支持纯图片和图文消息，并修复模型 402 误分类、历史识图任务错误回补、双实例版本不可追溯等已确认问题，同时保持 CArroT/Kona 的配置、数据、凭据与发布节奏隔离。

**Architecture:** 复用现有 `chat_vision` 下载与识别能力、`random_chat.generate_reply(images=...)` 视觉输入和 `private_memory` SQLite 生命周期。私聊原图只在当前请求内存中短暂存在，不写入群聊图片表或本地文件；仅把受限长度的图片文字描述写入私聊消息，以便重启后恢复上下文。LLM Gateway 对 HTTP 402 提供独立、不可重试的错误分类，识图恢复任务仅处理仍有价值且可重试的任务。候选版本先以可追溯 Git SHA 部署 CArroT，人工验收后才把同一 SHA 推广至 Kona。

**Tech Stack:** Python 3.11、NoneBot2、OneBot V11、SQLite、httpx、unittest、systemd、现有原子发布脚本。

---

## 边界与不变量

- 只有私聊白名单用户可以触发私聊图片读取和私聊记忆。
- CArroT 与 Kona 使用同一代码提交，但 `.env`、运行时开关、数据库、表情包、人设和 API Key 始终独立。
- 私聊原图和供应商图片 URL 不落盘、不写数据库；数据库只保存文字描述。
- 私聊图片描述是低权限上下文数据，不能成为业务指令，也不能进入违规判断。
- 私聊视觉关闭时，图文消息仍可按文字回复；纯图片消息不允许凭空猜测。
- HTTP 402 不重试，不记录供应商响应正文或密钥。
- 已由余额不足导致且超过恢复边界的旧视觉任务不自动补发、补识别或补回复。
- 不修改现有违规业务规则，不将 Kona 的独立配置或数据覆盖到 CArroT，反之亦然。

## Task 1: 私聊图片输入契约（TDD）

**Files:**
- Create: `plugins/private_chat/vision.py`
- Modify: `plugins/private_chat/matcher.py`
- Test: `tests/test_private_chat.py`（若现有私聊测试位于其他文件，则扩展现有文件）

1. 先写失败测试，覆盖：纯图片、文字加图片、最多四张、总字节预算、非白名单、视觉关闭、下载失败、不同用户隔离。
2. 运行聚焦测试并确认因缺少实现而失败。
3. 实现图片段提取、受限下载、当前轮原始视觉输入和安全降级。
4. 保证下载后的原图不写入 `chat_image_assets`，也不创建本地图片文件。
5. 重新运行聚焦测试直至通过。

## Task 2: 私聊图片描述持久化与幂等迁移（TDD）

**Files:**
- Modify: `plugins/private_memory/schema.py`
- Modify: `plugins/private_memory/store.py`
- Modify: `plugins/private_chat/conversation.py`
- Test: `tests/test_private_memory_schema.py`
- Test: `tests/test_private_memory_store.py`
- Test: corresponding private-chat integration tests

1. 先写 v2 到 v3 迁移失败测试，要求新增 `image_descriptions_json`、保留原行、重复迁移无副作用。
2. 写存取失败测试，覆盖正常描述、纯图片占位、损坏 JSON fail-closed、长度/数量限制和跨用户隔离。
3. 将 `PRIVATE_MEMORY_SCHEMA_VERSION` 升至 3，并以增量列迁移实现幂等升级。
4. 让 `ContextMessage.image_descriptions` 随私聊原文持久化并恢复到近期聊天上下文；原图与 URL 不持久化。
5. 图片描述首版不进入事实抽取、关系更新或滚动摘要，避免把视觉模型观察误当成用户明确事实。
6. 运行迁移、存储和处理器聚焦测试。

## Task 3: HTTP 402 精确分类与不可重试语义（TDD）

**Files:**
- Modify: `plugins/llm_gateway/errors.py`
- Modify: `plugins/llm_gateway/transport.py`
- Modify: `plugins/private_memory/ai.py`
- Modify: `plugins/chat_vision/client.py`
- Test: `tests/test_llm_gateway_transport.py`
- Test: `tests/test_llm_gateway_memory_migration.py`
- Test: `tests/test_llm_gateway_vision_migration.py`

1. 先写失败测试，断言 402 映射为 `GatewayPaymentRequiredError` 且不可重试。
2. 写视觉与记忆适配层测试，要求保留安全错误分类但不泄露响应正文。
3. 实现错误类型、映射、脱敏用量记录和安全日志。
4. 运行相关聚焦测试。

## Task 4: 旧识图任务恢复熔断（TDD）

**Files:**
- Modify: `plugins/chat_vision/service.py`
- Modify: `plugins/chat_vision/lifecycle.py`
- Modify: chat vision schema/store only if required by the smallest safe design
- Test: `tests/test_chat_vision_ingestion.py`

1. 先写失败测试，覆盖不可重试错误、过期任务、启动恢复上限和新任务不受影响。
2. 将 `payment_required` 任务标为不可自动恢复；启动恢复必须有时间/数量边界，不能无限回补历史图片。
3. 为生产中已确认的 402 批次准备一次性、可审计、事务化处理；执行前必须在线备份数据库。
4. 运行识图恢复聚焦测试。

## Task 5: 双实例状态与发布追溯修复

**Files:**
- Modify: existing deployment documentation/scripts only where necessary
- Modify: `README.md` and `CHANGELOG.md`

1. 记录当前两个 release 目录不能解析为 Git 对象的问题，不把它们冒充可追溯提交。
2. 确认 CArroT/Kona 当前配置与运行时开关差异；只修复确定是漂移的项，不覆盖有意隔离项。
3. 将“私聊纯图片不再忽略”“原图不持久化”“402 不重试”“人工推广同一 SHA”写入文档。
4. 保持候选发布脚本的干净工作树、完整测试、公开树扫描、原子切换和自动回滚要求。

## Task 6: 验证与 CArroT 候选部署

1. 运行所有相关单元测试并确认失败会返回非零状态。
2. 使用有效测试环境运行完整测试套件。
3. 运行 `compileall`、`git diff --check`、公开仓库当前树和历史扫描。
4. 在临时数据库验证 v2→v3、重复迁移以及回滚备份可读。
5. 对生产 CArroT 数据库做在线备份；只处理已证实的 402 旧任务批次。
6. 将候选代码形成可追溯提交并通过现有原子发布流程部署到 CArroT。
7. 验证服务、OneBot 连接、数据库版本、私聊文字和新图片路径；如果 CArroT 账户仍无余额，明确把真实模型行为列为外部阻塞，不伪称通过。
8. 保留发布前 release symlink 和数据库备份作为回滚点。

## Task 7: 人工验收后推广 Kona

1. 等待用户明确确认 CArroT 候选行为。
2. 将同一 Git SHA 推送/合并到约定远端，并通过现有推广流程部署 Kona；不重新打包另一份未提交代码。
3. 只运行 Kona 自己的迁移、配置和数据库，不复制 CArroT 运行数据。
4. 验证 Kona 的私聊白名单、群白名单、聊天模式、图片输入和 Prompt Builder 运行时状态。
5. 若任一验证失败，原子切回上一 release 并保留迁移前数据库备份。
