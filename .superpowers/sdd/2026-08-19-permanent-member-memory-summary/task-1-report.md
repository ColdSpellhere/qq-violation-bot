# Task 1 报告：追加式原始记忆与昵称账本

## 实现
- 在 plugins/member_memory/store.py 增加永久事实、历史昵称和摘要状态表，以及统一的 _ensure_schema。
- member_memory_facts 使用唯一键幂等追加，新增事实计数只统计实际插入记录。
- member_memory_aliases 永久保存旧昵称并去重；旧 aliases_json / traits_json 仅作为最近 8 条兼容视图。
- MemberProfile / MemoryTrait 增加事实 ID、摘要字段。
- 本地 JSON mirror 每次从 SQLite 账本读取并写入完整事实、完整别名及摘要状态。
- 修改存储测试，覆盖 10 条事实、10 次改名和重复候选幂等性。

## RED 证据
命令：/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory.MemberMemoryStoreTests -v

实现前结果：2 项失败。10 条事实实际仅应用 8 条；10 个昵称仅保留 8 个历史别名。

## GREEN / 验证证据
命令：/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory.MemberMemoryStoreTests -v

结果：3 tests，全部通过（OK）。

另行通过：
- /opt/qq-violation-bot/.venv/bin/python -m py_compile plugins/member_memory/store.py
- git diff --check

## 变更文件
- plugins/member_memory/store.py
- tests/test_member_memory.py

## 自审
- 权威事实和昵称账本没有自动删除或数量截断。
- 重复事实由 SQLite 唯一约束拦截，重复处理返回 0。
- 旧字段保留最近 8 条，完整历史由账本和 mirror 保留。
- 未运行全量测试，符合任务要求；摘要业务流程不在 Task 1 范围内。


## Fix round 1

### 修复内容
- 首次更新已有 legacy profile 时，在同一 SQLite 事务中将 aliases_json/traits_json 幂等导入 append-only ledger，再执行昵称/事实更新。
- mirror 原子替换失败现在只记录异常日志，不影响已提交的 SQLite 数据和后续业务。
- 清理重复的 TypeError 异常类型。
- 新增旧库升级与 mirror 失败回归测试。

### RED 证据
新增 test_legacy_profile_is_imported_into_append_only_ledger_before_update 后运行存储测试，旧实现失败：legacy aliases 未完整保留（实际只有更新时追加的旧昵称，缺少原 aliases_json 内容）。

### GREEN / 验证证据
命令：
/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory.MemberMemoryStoreTests -v

结果：5 tests，全部通过（OK），包括：
- legacy profile 幂等导入后保留 2 个旧别名、2 条旧事实和 1 条新事实；
- 重复候选不重复插入；
- mirror 写入失败时 SQLite profile 仍可读取。

另行通过：
- /opt/qq-violation-bot/.venv/bin/python -m py_compile plugins/member_memory/store.py
- git diff --check


## Fix round 2

### 修复内容
- 将 mirror 目标目录创建纳入 OSError 防护；目录创建失败只记录日志并返回，不向业务调用方传播。
- 新增 test_mirror_directory_failure_does_not_escape，验证 SQLite profile 仍可读取。

### RED / GREEN 证据
RED：新增目录创建失败测试在修复前以 OSError 失败。
GREEN：/opt/qq-violation-bot/.venv/bin/python -m unittest tests.test_member_memory.MemberMemoryStoreTests -v 结果为 6 tests，全部通过（OK）。
另行通过 py_compile plugins/member_memory/store.py 与 git diff --check。
