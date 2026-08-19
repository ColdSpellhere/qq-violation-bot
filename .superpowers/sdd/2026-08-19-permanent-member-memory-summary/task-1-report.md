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
