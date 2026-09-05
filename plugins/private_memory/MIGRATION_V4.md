# 记忆与归档修复：schema 4

本变更仅涉及聊天归档、群/私聊记忆及治理；不修改分词、语义理解、计数等业务模块。

## 行为与接口

- `archive_payload_async(path, target_group_id, payload)` 是异步调用入口，等待 SQLite 提交；协程取消时仍等待已经开始的归档完成。返回 `True` 仅表示新行插入，重复或目标群不匹配返回 `False`。同步 `archive_payload` 接口保留。
- `chat_messages` 身份由全局 `message_id` 改为 `(group_id,message_id)`；迁移保留原 `rowid`、正文、引用字段。原读取接口保持不变。
- 群记忆生产入口持久写入 `memory_jobs` 的 `member_facts` 类型，默认同成员累计 5 条或 60 秒后处理。进程重启继续使用原队列租约和重试机制。
- 私聊事实使用 `private_fact_progress`，群事实使用 `group_fact_progress`。事实写入与输入水位推进处于同一事务；失败不跳过来源。私聊清理推进并增加 facts 水位版本，防止在途任务重新写入旧事实。
- 默认每轮处理最多 20 条、12000 字符；长文本有界截取。群摘要每次队列轮转最多生成一次；积压通过 `MemoryJobContinuation.MORE` 继续，不消耗失败次数。私聊摘要也按有界批次推进。管理员更改事实或在模型执行期间清理来源，会使旧快照提交失败。
- 关系更新合并同 scope 的待执行消息，默认安静 60 秒后执行，持续输入最长等待 300 秒。最新消息保留在有界上下文中。`MemoryJob.input_from_id` 可选默认 0，旧任务沿用原来的单条来源语义。
- 事实、摘要和关系写入前检查凭据标签/常见密钥格式；不会自动删除已有长期事实。该规则是保守过滤，不保证识别任意未标识的随机秘密。
- 治理清理仍保留既有长期事实及关系主体，只清空原有短期层；同事务取消该用户私聊投递计划、清空回复/回执/错误正文并保留不透明去重键。长事实的删除继续使用单条治理操作。
- 治理预览正文默认在消费或过期 7 天后清空，保留操作目标、哈希、审计记录等元数据；启动和每日保留清理执行。WAL 被读者占用时可能暂存旧页，后续周期继续回收。
- SQLite、镜像更新及治理操作不在消息事件循环中执行；队列处理期间续租。镜像仍以数据库为准，使用有界锁及独立临时文件避免并发写入冲突。

新增参数均有模块默认值，无必须增加的环境变量：`MemoryJobQueue` 的 `relationship_debounce_seconds=60`、`relationship_max_wait_seconds=300`、`member_batch_delay_seconds=60`、`member_batch_threshold=5`；`PrivateMemoryProcessor` 的 `batch_messages=20`、`batch_chars=12000`；`prune_previews(..., retention_days=7)`。

## 迁移与回退

1. 对实际数据库的一致备份副本先运行迁移；核对 `integrity_check`、各表行数、原主键/rowid、关系来源引用、jobs 租约状态和 `sqlite_sequence`。禁止用生产库执行离线单元测试。
2. 正式切换前停止写入，取得最后一致备份，同时保存旧代码/运行配置/镜像。现有启动流程会先检查再建立迁移备份。迁移只在版本切换时运行，不在每次归档时隐式重建旧表。
3. 迁移在同一事务中扩展 schema。`memory_jobs` 增加 `member_facts` 类型、`input_from_id`、`input_count`，保留 id/重试/租约与自增高水位；创建两张事实进度表。仅成功的旧私聊 facts 作业初始化私聊水位。
4. 归档出现未知列或主键、依赖 view/trigger/外键，以及 jobs 出现未知列/依赖时，迁移拒绝执行并整体回滚，需先人工评估。不能因为文件存在便认定迁移完成，应核 schema version 4。
5. 旧版本服务会拒绝 schema 4。因此回退必须停止新写入，成套恢复相匹配的代码、数据库及相关状态。运行新版本后产生的新数据必须先导出/评估合并；不能自动覆盖恢复旧备份，不能仅把版本号改回 3。

保留完整历史 facts 与终态 jobs，避免不可逆清理影响审计。失败批次可以通过只读查询定位，再经审查决定是否重试：

```sql
SELECT job_type,status,error_code,COUNT(*)
FROM memory_jobs GROUP BY job_type,status,error_code;
SELECT job_type,status,MIN(next_run_at),MAX(updated_at),COUNT(*)
FROM memory_jobs WHERE status IN ('pending','running','failed')
GROUP BY job_type,status;
```

## Kona 移植

保留 Kona 原本的人设与非记忆定制。尤其旧 Kona `extract_private_facts` 直接调用 `_legacy_complete`，而 Carrot 使用 `_complete(task='private_facts', ...)` 网关分流；移植本修复时应同时核对 root 的网关修复，不能只复制 processor/store 就声称所有事实请求已进入网关统计和预算。

## 离线验收

`tests/fixtures/memory_schema_v3.sql` 来自 Carrot 基线 a37b1a6 的表定义，所有测试数据为合成数据。新增测试覆盖历史 tombstone、小于正常阈值的摘要重建、保留清理前后与在途冲突、跨重启增量水位、原子回滚、有界批处理、群队列重试、关系去抖及续租、凭据过滤、治理预览正文过期、私聊投递清理、旧 schema 迁移和异步归档取消语义。未调用真实 QQ 或外部模型；生产副本迁移与实际运行检查由部署整合阶段执行。
