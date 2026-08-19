# QQ 群违规记录机器人

基于 Napcat + Nonebot2 的 QQ 群违规记录与群管理助手。机器人只处理允许群内 @ 机器人的消息，允许群成员会自动登记为可操作 admin，业务理解优先交给 AI Intent Router，再由后端用严格字段校验、成员解析、状态锁定和二次确认执行。群禁言会直接调用 OneBot/Napcat 的群管理接口。

## 技术栈

- Python 3.10+
- Nonebot2
- nonebot-adapter-onebot v11
- Napcat QQ
- SQLite
- DeepSeek/OpenAI-compatible Chat Completions API

## 目录结构

```text
/opt/qq-violation-bot
├── bot.py
├── requirements.txt
├── .env.example
├── data/
├── exports/
├── backups/
├── logs/
├── scripts/
│   ├── import_violation_xlsx.py
│   ├── manage_admin.py
│   ├── start_bot.sh
│   └── backup_db.sh
└── plugins/violation_record/
    ├── matcher.py
    ├── config.py
    ├── ai_router.py
    ├── schemas.py
    ├── validators.py
    ├── service.py
    ├── db.py
    ├── member_resolver.py
    ├── admin_resolver.py
    ├── exporter.py
    ├── scheduler.py
    └── formatter.py
```

## 环境要求与安装

```bash
cd /opt/qq-violation-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

## .env 配置

```dotenv
DRIVER=~fastapi
HOST=127.0.0.1
PORT=6199
LOG_LEVEL=WARNING
TARGET_GROUP_ID=123456789
BOT_SELF_ID=1234567890
NAPCAT_ACCESS_TOKEN=replace-with-random-token
EVIDENCE_REQUIRED=false
EVIDENCE_MAX_BYTES=20971520
MUTE_ENABLED=false
DEDUCTION_POLICY_V102_ENABLED=false
DEDUCTION_POLICY_RULE_VERSION=v1.0.2beta
DATABASE_URL=sqlite:////opt/qq-violation-bot/data/violation_records.db
AI_BASE_URL=https://api.deepseek.com
AI_API_KEY=你的 DeepSeek Key
AI_MODEL=deepseek-chat
RANDOM_CHAT_ENABLED=false
RANDOM_CHAT_PROBABILITY=0.10
PRIVATE_CHAT_ENABLED=false
PRIVATE_CHAT_ALLOWED_USER_ID=
ADMIN_SEED=123456:ColdSpell:冷|spell;654321:企鹅
```

`AI_API_KEY` 缺失时，机器人会回复：`AI 未启用或缺少 AI_API_KEY，无法进行自然语言解析。`

`RANDOM_CHAT_ENABLED=false` 默认关闭随机 AI 群聊回复。设为 `true` 后，只在当前 `TARGET_GROUP_ID` 对普通成员文字按 `RANDOM_CHAT_PROBABILITY` 概率回复，默认值 `0.10` 表示 10%。命中后会读取当前群最近 30 分钟内最多 20 条纯文本，按群名片、QQ 昵称、QQ号的顺序标注成员并交给 AI 理解上下文；不读取图片或业务数据库。机器人自身消息、@ 机器人消息、空消息和 `/` 开头命令不会触发；归档、AI 或发送异常时静默降级，不影响违规记录等现有模块。紧急停用时将开关恢复为 `false`，再执行 `systemctl restart qq-violation-bot.service`。

`TARGET_GROUP_ID` 只允许配置一个群号。其他群的消息在框架接收入口后立即丢弃，不进入 NLP、业务查询、数据库写入、消息归档、图片下载、管理员同步或自定义日志。目标群的全部消息都会归档，不要求必须 @ 机器人；归档保存消息及相关元数据，但不会因此下载消息中的普通图片。

`RANDOM_CHAT_ENABLED=true` 时，萝卜猫以 `RANDOM_CHAT_PROBABILITY` 概率尝试参与目标群普通聊天；当前生产运行配置为 `0.03`（3%）。萝卜猫只是她的名字，她不是猫，不自称猫或使用“喵”等猫系口癖；她喜欢花和植物，并把“反二梦女”视为自己的兴趣和自我标签。上下文会保留发送者 QQ、昵称、艾特对象和引用对象，避免把群友之间的话误当成对机器人说。

`RANDOM_CHAT_DIRECT_FALLBACK_ENABLED=true` 允许点名或回复机器人、但业务路由判定为 `unknown` 的消息转为正常闲聊；查询、记录、减数、禁言等已识别业务仍优先处理。若该功能异常，将它改为 `false` 并重启 `qq-violation-bot.service` 即可关闭，无需回滚版本。成功的闲聊回复以 `RANDOM_CHAT_STICKER_PROBABILITY=0.20` 的概率附带最多一张表情包；指定首图在已决定附图时占 10%，其余图片均分 90%。表情包只保存在 `data/random_chat/stickers/incoming/`，不会提交到 GitHub，业务回复永不附图。

`PRIVATE_CHAT_ENABLED=true` 时，机器人只回复 `PRIVATE_CHAT_ALLOWED_USER_ID` 指定的 QQ 私聊白名单，多个 QQ号使用英文逗号分隔，其他账号完全静默。允许账号的每条非空普通文字都会由萝卜猫回复，不使用群聊 3% 概率；以 `/` 开头的命令和纯图片消息忽略。最近 20 条双方文字只保存在进程内存中，服务重启即清空，不写入群归档、成员记忆或业务数据库。私聊回复沿用 20% 表情包概率。紧急关闭时将 `PRIVATE_CHAT_ENABLED=false` 并重启机器人服务，无需回滚代码。

成员记忆独立于随机回复概率持续收集。原始特性和历史昵称以追加式账本永久保存在服务器 SQLite 中，不再按 8 条上限淘汰；本地 JSON 镜像包含完整历史。每累计 5 条新特性会生成一次不超过 300 字的滚动摘要，聊天 AI 只读取摘要、最多 5 个近期旧称和最多 8 条尚未摘要的特性。`MEMBER_MEMORY_SUMMARY_ENABLED=false` 只关闭摘要生成，永久账本仍继续写入。真实成员记忆与镜像不会提交到 GitHub。

`EVIDENCE_REQUIRED=false` 表示新增违规记录未引用证据图片时只提醒、不阻止现有记录流程；改为 `true` 后才要求提供证据。只有新增违规命令所引用消息中的图片会被下载并持久化，每张图片最大允许 `EVIDENCE_MAX_BYTES` 字节。查询时，每条违规记录的文字和该记录映射的全部图片会尽量通过同一条 OneBot 混合消息发送；混合消息发送失败时会回退为文字和图片分别发送。旧记录没有证据图片时仍可正常查询。

`MUTE_ENABLED=false` 默认关闭群禁言执行；启用后才会调用 OneBot/Napcat 的群管理接口。该开关不改变现有违规记录、查询和确认流程。

`DEDUCTION_POLICY_V102_ENABLED=false` 默认保留原每周减计机制。准确数据完成预演、迁移和回滚演练后才可改为 `true`；启用后只运行 v1.0.2beta 策略引擎，旧减计任务停止结算，避免重复减数。`DEDUCTION_POLICY_RULE_VERSION` 用于事件审计，生产环境应保持为发布版本 `v1.0.2beta`。

## Napcat 与 Nonebot 连接

在 Napcat 中配置 OneBot v11 反向 WebSocket，连接到 Nonebot 地址：

```text
ws://127.0.0.1:6199/onebot/v11/ws
```

Napcat token 在 Napcat 的 OneBot/网络配置中设置，需与 `.env` 的 `NAPCAT_ACCESS_TOKEN` 保持一致。不同 Napcat 版本配置界面名称略有差异，关键是启用 OneBot v11 反向连接并指向上面的 WebSocket 地址。

启动机器人：

```bash
cd /opt/qq-violation-bot
.venv/bin/python bot.py
```

或：

```bash
bash scripts/start_bot.sh
```

验证收到群消息：在 `TARGET_GROUP_ID` 配置的群里发送 `@违规记录助手 帮助`。其他群、未 @ 机器人的普通消息会被忽略。

## 管理员列表维护

允许群内成员会自动写入并激活到 `admins` 表，因此不需要逐个手动开放权限。机器人第一次收到允许群里的 @ 消息时，会尝试同步该群成员列表；如果同步失败，也会至少登记当前操作人。

`ADMIN_SEED` 和脚本仍可用于预设昵称、别名或手动维护：

```bash
cd /opt/qq-violation-bot
.venv/bin/python scripts/manage_admin.py add 123456 ColdSpell --aliases 冷,spell
.venv/bin/python scripts/manage_admin.py list
```

管理员以 `admins.qq_number` 作为唯一标识。记录人固定使用发送当前消息的 QQ号；处理人如果在指令中指定 QQ号，会优先按 QQ号精确匹配，未指定时默认等于记录人。昵称只用于处理人未写 QQ号时的辅助匹配，匹配范围包含 `admins.nickname/aliases`。

机器人同步到同一 QQ号的新群名片/昵称时，会把旧昵称自动保留到 `aliases`，避免昵称变更后旧叫法立刻失效；历史导入记录中只有文本、没有 QQ号的处理人仍需要人工维护别名或映射。

## QQ昵称与群昵称

展示首行固定为 `QQ昵称（QQ号）`。不要把群昵称当作 QQ昵称保存。Napcat 若无法稳定提供 QQ 昵称，则由记录人手动输入，例如 `蜂巢小明（123456）...`。后续展示使用数据库保存的 QQ昵称，缺失时显示 `未知昵称（123456）`。

## 自然语言示例

记录、查询、状态等数据业务除 `帮助`、`确认`、`取消` 外，必须包含 `蜂巢 / 蜂窝 / 蜂箱`。群禁言作用于当前 QQ 群，不需要分区。

- 记录：`@机器人 蜂巢小明（123456）2026/6/14 0:00刷屏，禁言，企鹅处理`
- 相对时间记录：`@机器人 蜂巢小明（123456）5分钟前刷屏，禁言，企鹅处理`
- 群禁言：`@机器人 禁言 @小明 10分钟`、`@机器人 把 123456 禁言半小时`、`@机器人 让 @小明 安静一会儿`
- 查询：`@机器人 查蜂巢小明`、`@机器人 小明蜂巢有几次违规`
- 分区记录：`@机器人 蜂巢本月违规记录`、`@机器人 蜂巢最近违规记录`
- 最近：`@机器人 查蜂巢123456最近`
- 质询：`@机器人 蜂巢质询123456 2026/6/1 12点`
- 最后警告：`@机器人 蜂巢最后警告123456 2026/6/1 12点`
- 撤回：`@机器人 撤回蜂巢123456记录`
- 状态：`@机器人 蜂巢123456退群`、`@机器人 移出蜂巢123456`、`@机器人 拉黑蜂巢123456`
- 解锁：`@机器人 蜂巢123456解锁`
- 导出：`@机器人 导出蜂巢违规记录`、`@机器人 导出蜂巢本月违规记录`、`@机器人 导出蜂巢日志 csv`
- 引用时间：回复/引用一条群消息后发送 `@机器人 蜂巢小明（123456）刷屏，禁言`，机器人会把被引用消息的时间作为违规时间进入预览。

## 业务流程

新增记录：AI 解析自然语言，后端校验群聊分区、成员、时间、原因、处理措施。写入前返回预览，操作人回复 `@机器人 确认` 后入库，回复 `@机器人 取消` 放弃。禁言未写时长默认 `禁言10分钟`，警告不计入次数。时间支持 `刚刚`、`5分钟前`、`两分钟前`、`半小时前`、`1小时20分钟前`、`昨天晚上8点`、`今天下午3点`、`今晚8点` 等相对表达，并按当前服务器时间格式化；`几分钟前` 这类没有精确数值或精确时刻的表达不会被编造，会提示补充准确时间。

群禁言：默认由 `MUTE_ENABLED=false` 关闭。启用后，机器人 QQ 号必须已经是当前群的管理员。管理员 @ 机器人后，用自然语言表达要禁言谁和多久即可；目标支持 @ 成员或 QQ号，时长支持 `10分钟`、`半小时`、`1小时20分钟`、`一天` 等表达，未写时长默认 10 分钟，最长 30 天。执行时调用 OneBot v11 `set_group_ban`，成功后写入 `operation_logs`。

查询：支持 QQ号 直查和 QQ昵称模糊查询。命中多人会返回候选项，不会误操作。回复展示当前次数、状态和倒序记录，不展示后台的总次数和减数。也支持不带 QQ号的分区查询，例如 `蜂巢本月违规记录`，用于查看该分区某个时间范围内的记录。

最近记录：以最后一条违规记录时间为终点，往前 14 天展示，精确到分钟。

质询/最后警告：需要二次确认。质询默认结果为 `通过`，状态变为 `已质询`；最后警告状态变为 `最后警告`。如果结果标明 `已移出` 或 `已拉黑`，会进入锁定状态。若状态变化由某条刚录入的违规记录直接引起，发送状态指令时应引用当时的原始记录消息；预览会显示关联记录编号。以后撤回该误记录时，明确关联的状态和原计时同步回退；未引用记录的独立人工状态不会被推断为关联。

撤回：软撤回最近一次有效记录，不物理删除，并写日志。

状态锁定与解锁：`已移出 / 已拉黑 / 已退群` 会锁定数据，锁定后只允许查询。解锁需要二次确认并写日志。

## 减除机制

系统保留事件表作为事实来源，同时在 `member_group_states` 中维护 `total_count / deduct_count / current_count_cache`。当前次数统一由 service 计算：有效记录数减去减数。撤回、测试、警告不计入有效次数。

v1.0.2beta 使用独立事件账本和周期状态：普通周期为 14 天；当前次数达到 3 自动进入减缓，减缓期从 21 天起、每次重新进入增加 7 天。减缓期第二条轻度禁言延长 7 天，第三条轻度禁言或任意严重禁言生成减停建议。禁言 1 小时及以上按严重违规识别；警告不计为违规。

普通减停以固定 30 天端点运行，到期后只提醒管理组决定，不自动解除；等待期间每小时提醒。最后警告进入独立 90 天周期，期间新增任何禁言都会生成移出提醒；无新增禁言时恢复为已质询并请求减数 2。成功执行减数的操作次数最多为 5 次，该上限与每次减数值相互独立。

策略固定命令在目标群内、通过 @ 机器人使用：

```text
减停 蜂巢 123456 事由：持续违规
清除减停 蜂巢 123456 事由：周期内表现良好
续期减停 蜂巢 123456 事由：周期内表现不良
拒绝减停建议 蜂巢 123456 事由：管理组复核不成立
查询减数状态 蜂巢 123456
查询减缓名单
查询减停名单
查询减停建议名单
查询减数待办
查询减数日志 蜂巢 123456
```

前四项为写操作，继续沿用 `确认 / 取消` 二次确认；所有人工决定必须填写事由。成功、失败、取消和过期结果都会写入操作日志。周期结算不依赖 NapCat，通知会先写入持久化 outbox，发送前复核来源事件和待办状态，连接恢复后再发到唯一目标群。

迁移工具支持只读预演、应用、验证、快照回填和逻辑回滚。`--apply` 与 `--repair-snapshots` 都必须同时提供 `--snapshot-database` 指向的切换前数据库文件及其真实 SHA-256；工具会校验文件摘要、数据库完整性、业务数据或切换水位，并在快照修复完成前拒绝启动策略或执行逻辑回滚。基线初始化事件只保留审计，不进入群通知；其他通知每分钟最多发送 10 条。

## 日志、备份、导出、周报

所有写操作和查询都会记录到 `operation_logs`。日志字段包括操作类型、操作人、目标成员、分区、前后数据、时间、来源和备注。

数据库初始化或迁移前会自动备份旧库。后台每周一、周四、周日执行整库备份，文件在 `backups/`，文件名含精确时间。手动备份：

```bash
bash scripts/backup_db.sh
```

导出支持 CSV/XLSX，文件名包含分区和精确时间，生成在 `exports/`。导出后机器人会优先调用 Napcat `upload_group_file` 上传到群文件；如果群文件权限、风控或 Napcat 能力导致上传失败，会返回服务器路径和失败原因。

每周日 00:10 生成 XLSX 周报，包含本周操作日志和当前各分区成员统计；启用 v1.0.2beta 后还会附加“减数策略日志”“减数待办”“通知发送历史”和“状态联动作业”。生成后会尝试上传到群文件，失败时返回服务器路径。

## NapCat 资源监控与定时重启

`qqbot-napcat-watchdog.timer` 每 5 分钟检查一次 `napcat.service` systemd cgroup 内的进程。任一 QQ/Node 进程文件描述符达到 `1500`、重复打开 `/proc/<pid>/maps` 的文件描述符达到 `1000`、任一 Xvfb 进程文件描述符达到 `220`，或最近 10 分钟出现 `Maximum number of clients reached` 时触发恢复。反向 WebSocket 未建立必须连续检查失败两次才触发恢复。每次实际重启后有 30 分钟冷却期，避免重复重启。

`qqbot-napcat-daily-restart.timer` 每天 04:10 请求一次计划重启，并在备份服务之后运行。触发恢复时只重启 `napcat.service`，不会重启 `qq-violation-bot.service`。重启后最多等待 90 秒，检查 NapCat、机器人服务、反向 WebSocket 和资源指标是否恢复。

查看两个 timer 的加载、启用和运行状态：

```bash
systemctl status qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
```

只采集当前指标和决策，不执行重启：

```bash
cd /opt/qq-violation-bot
.venv/bin/python scripts/napcat_watchdog.py --check-only
```

查看当天 watchdog 执行记录：

```bash
journalctl -u qqbot-napcat-watchdog.service --since today --no-pager
```

回滚自动恢复策略时，立即停止并禁用两个 timer；此命令不会停止机器人或 NapCat：

```bash
systemctl disable --now qqbot-napcat-watchdog.timer qqbot-napcat-daily-restart.timer
```

## 历史 XLSX 导入

导入脚本：

```bash
cd /opt/qq-violation-bot
.venv/bin/python scripts/import_violation_xlsx.py /opt/import/蜂巢违规记录.xlsx --dry-run
systemctl stop qq-violation-bot.service
.venv/bin/python scripts/import_violation_xlsx.py /opt/import/蜂巢违规记录.xlsx
systemctl start qq-violation-bot.service
```

脚本会先自动备份 SQLite，再清洗并导入 Excel。已兼容样式损坏的 XLSX：如果 `openpyxl` 读取样式失败，会生成去样式临时副本继续解析。当前导入规则：

- `蜂巢 / 蜂窝 / 蜂箱` 工作表按明确分区导入。
- `低频小于三 / 暂存` 按旧表结构推断为蜂巢历史数据，并写入来源标记。
- Excel 日期序列和文本日期都会格式化为 `YYYY-MM-DD HH:MM:SS`。
- QQ号会清洗全角数字、括号和异常前导 0；QQ昵称保存为成员昵称，不使用群昵称表。
- 原表“总次数”按当前次数处理；脚本会导入历史事件，并用 `deduct_count` 保持机器人查询出的当前次数贴近原表。
- `测试` 表只导入能明确识别 `蜂巢 / 蜂窝 / 蜂箱` 的质询/拉黑状态；没有分区的黑名单会跳过并写入报告。
- 重复执行时会按成员、分区、时间、判定、处理措施和备注这些业务字段跳过已导入记录。

导入报告在 `import_reports/`，包括 JSON 报告和本次导入记录 CSV。导入为历史后台操作，会写入 `operation_logs`。

## 数据库迁移

本项目启动时会 `CREATE TABLE IF NOT EXISTS` 并清理旧版本遗留的导入追踪列；迁移前自动备份旧数据库到 `backups/db_backup_before_migrate_*.sqlite3`。如果从旧版本手动迁移，需要先保留旧库文件，再把旧记录按字段导入 `members`、`admins`、`member_group_states`、`violation_records`。

新增表：

- `members`
- `admins`
- `member_group_states`
- `violation_records`
- `consultation_records`
- `operation_logs`
- `pending_operations`

旧版导入追踪字段会被移除，包括 `import_*`、`raw_*` 和 `*_name_text`。迁移会先把旧的处理人文本合并进 `remark`，再删除这些列。

## 常见问题

- 机器人不回复：确认消息来自 `TARGET_GROUP_ID`，并且 @ 的是 `BOT_SELF_ID` 对应机器人。
- 提示缺少群聊：业务指令必须写 `蜂巢 / 蜂窝 / 蜂箱`。
- AI 解析失败：检查 `AI_API_KEY`、`AI_BASE_URL`、服务器网络。
- 处理人匹配不到：先在允许群里 @ 机器人触发群成员同步，或用 `scripts/manage_admin.py list/add` 检查管理员昵称和别名。
- QQ昵称查不到：先用 `QQ昵称（QQ号）` 新增一条记录，或后台维护 members 表。
- 已锁定不能新增：先查询确认状态，再用解锁指令或后台修改。
- 禁言失败：确认机器人 QQ 是群管理员，目标在本群内，且目标不是群主或权限高于机器人。
