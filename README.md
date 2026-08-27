# QQ 群违规记录机器人

基于 Napcat + Nonebot2 的 QQ 群违规记录与群管理助手。业务路径只处理 `TARGET_GROUP_ID` 内 @ 机器人的消息；允许群成员会自动登记为可操作 admin，业务理解优先交给 AI Intent Router，再由后端用严格字段校验、成员解析、状态锁定和二次确认执行。群禁言会直接调用 OneBot/Napcat 的群管理接口。聊天路径由 `CHAT_ENABLED`、`GROUP_CHAT_ENABLED` 和群聊白名单控制：白名单群内明确 @ 机器人会进入聊天回复，普通消息仍按 `RANDOM_CHAT_PROBABILITY` 概率处理。

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
AI_TIMEOUT=30
LLM_GATEWAY_ENABLED=false
PROMPT_BUILDER_ENABLED=false
LLM_GATEWAY_VISION_ENABLED=false
LLM_GATEWAY_PRIVATE_MEMORY_ENABLED=false
LLM_GATEWAY_MEMBER_MEMORY_ENABLED=false
LLM_GATEWAY_CHAT_ENABLED=false
LLM_GATEWAY_BUSINESS_ENABLED=false
LLM_GATEWAY_MAX_CONNECTIONS=8
LLM_GATEWAY_MAX_RETRIES=2
LLM_GATEWAY_TOTAL_CONCURRENCY=8
LLM_GATEWAY_BUSINESS_CONCURRENCY=2
LLM_GATEWAY_CHAT_CONCURRENCY=3
LLM_GATEWAY_VISION_CONCURRENCY=3
LLM_GATEWAY_MEMORY_CONCURRENCY=2
CHAT_VISION_ENABLED=false
CHAT_VISION_MODEL=deepseek-v4-flash-vision-exp
CHAT_VISION_IMAGE_ROOT=data/chat_vision/images
CHAT_VISION_RETENTION_DAYS=7
CHAT_VISION_MAX_BYTES=10485760
CHAT_VISION_TIMEOUT=60
CHAT_VISION_MAX_RETRIES=3
RANDOM_CHAT_ENABLED=false
RANDOM_CHAT_PROBABILITY=0.10
BUSINESS_ENABLED=true
CHAT_ENABLED=false
GROUP_CHAT_ENABLED=false
GROUP_CHAT_ALLOWED_GROUP_IDS=123456789
PRIVATE_CHAT_ENABLED=false
PRIVATE_CHAT_ALLOWED_USER_IDS=
PRIVATE_MEMORY_ENABLED=false
RELATIONSHIP_STATE_ENABLED=false
MEMORY_GOVERNANCE_ENABLED=false
PRIVATE_MEMORY_RETENTION_DAYS=30
PRIVATE_MEMORY_MAX_MESSAGES=500
PRIVATE_MEMORY_SHUTDOWN_TIMEOUT=10
# 仅为旧版私聊白名单兼容保留；新部署使用上面的多值配置
PRIVATE_CHAT_ALLOWED_USER_ID=
ADMIN_SEED=123456:ColdSpell:冷|spell;654321:企鹅
```

`AI_API_KEY` 缺失时，机器人会回复：`AI 未启用或缺少 AI_API_KEY，无法进行自然语言解析。`

### 群聊图片理解

`CHAT_VISION_ENABLED=false` 是安全默认值。启用后，插件只处理部署后实时到达、同时通过聊天总开关、群聊子开关和群聊白名单的真人群消息；不会扫描聊天归档、回填或重新识别历史图片。每条新消息内的每一张图片都会独立识别并保存简洁、事实性的中文描述，识别不依赖随机回复是否命中。

聊天图片原图只写入 `data/chat_vision/images/`，单图最大 `CHAT_VISION_MAX_BYTES=10485760` 字节；原图在 `CHAT_VISION_RETENTION_DAYS=7` 天后清理，已生成的文字描述、哈希和审计记录永久保留。视觉功能复用现有 `AI_BASE_URL` 和同一个 `AI_API_KEY`，只通过 `CHAT_VISION_MODEL` 选择视觉模型，不新增或复制另一份密钥。

服务启动只恢复 `CHAT_VISION_RECOVERY_WINDOW_SECONDS=900` 秒内、最多 `CHAT_VISION_RECOVERY_MAX_ASSETS=20` 个仍可重试的识图任务。HTTP 402 余额不足会记录为不可重试的 `payment_required`；过期任务、非群聊作用域和已确认不可重试的任务不会进入启动恢复。相同时间窗也会拒绝上游重新投递的历史消息绕过实时入口；最多允许 5 分钟的未来时钟偏差。这样充值或更换密钥后重启也不会突然回补大量旧图。两项恢复配置只接受正整数，非法值回到上述安全默认值，并分别硬限制为最多 1800 秒和 100 个任务。

未艾特机器人的纯图片消息仍按 `RANDOM_CHAT_PROBABILITY` 决定是否回复；明确艾特机器人且含图片时不走概率抽样。当前消息和引用消息的原图总数量不超过 4 张、总字节不超过 `CHAT_VISION_MAX_BYTES` 时，回复会直接使用全部可用原图；任一总预算超限时不会构造无界 Base64，而是改用每张图已经持久化的事实描述。图文混合消息继续沿用普通聊天概率，业务文字仍优先进入业务路由。当前或被引用的原图过期、缺失或视觉请求失败时，不会伪造“已看图”的回复；可用的永久描述仍可用于聊天上下文。

聊天视觉数据与违规证据硬隔离：视觉模块只使用 `data/chat_vision/images/` 和聊天归档数据库，不读取、迁移、索引、重新识别或清理 `evidence/`；证据数据库、证据文件及现有查询行为不受 7 天原图策略影响。

`RANDOM_CHAT_ENABLED` 是旧版首次群聊默认值兼容输入；运行时实际由下文的聊天总开关、群聊子开关和群聊白名单决定。允许群内，明确 @ 机器人的文字会直接进入聊天回复；普通成员文字仍按 `RANDOM_CHAT_PROBABILITY` 概率回复，默认值 `0.10` 表示 10%。命中后会读取当前群最近 30 分钟内最多 20 条消息，按群名片、QQ 昵称、QQ号的顺序标注成员并交给 AI 理解上下文；图片使用已持久化的事实描述，不重新下载历史原图，也不读取业务数据库。机器人自身消息、空消息和 `/` 开头命令不会触发。归档、AI 或发送异常时静默降级，不影响业务模块。

`TARGET_GROUP_ID` 只允许配置一个业务群号。只有该群会进入业务 NLP、业务查询、数据库写入和管理员同步；加入聊天白名单的其他群只进入聊天流程。允许聊天的群消息都会归档，不要求必须 @ 机器人；归档模块本身只保存消息及相关元数据，不负责下载图片。独立的聊天视觉插件在启用后会下载部署后实时到达的合格群聊图片，并按上面的 7 天策略管理原图。

#### 生产启用、验收与回滚

以下命令在服务器 `/opt/qq-violation-bot` 中以有权管理 `qq-violation-bot.service` 的账号执行。先把 `<release-commit>` 替换为已经审核的发布提交；如果工作区不干净、提交无法快进或任一检查不是 `ok`，立即停止发布，不要覆盖现场修改。

```bash
cd /opt/qq-violation-bot
set -euo pipefail
RELEASE_COMMIT="${RELEASE_COMMIT:?请先执行 export RELEASE_COMMIT=审核通过的完整提交号}"
CHANGE_ID="chat-vision-$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="backups/$CHANGE_ID"

test -z "$(git status --porcelain)"
git fetch origin
git rev-parse --verify "$RELEASE_COMMIT^{commit}"
PREVIOUS_COMMIT="$(git rev-parse HEAD)"
git branch "rollback/$CHANGE_ID" "$PREVIOUS_COMMIT"
install -d -m 0700 "$BACKUP_DIR"
printf '%s\n' "$PREVIOUS_COMMIT" > "$BACKUP_DIR/previous-commit.txt"

systemctl stop qq-violation-bot.service
cp -p .env "$BACKUP_DIR/env.before"
```

停服后使用 SQLite 在线备份接口复制业务库、聊天归档库和证据索引库。不存在的库会记录为 `existed: false`，回滚时不会误把它当成旧数据。

```bash
BACKUP_DIR="$BACKUP_DIR" .venv/bin/python - <<'PY'
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv('.env')
from plugins.violation_record.config import CONFIG

backup_dir = Path(os.environ['BACKUP_DIR'])
sources = {
    'business': CONFIG.database_path,
    'chat_archive': CONFIG.chat_archive_path,
    'evidence_index': CONFIG.evidence_database_path,
}
manifest = {}
for name, source in sources.items():
    source = Path(source).absolute()
    existed = source.is_file()
    manifest[name] = {'path': str(source), 'existed': existed}
    if not existed:
        continue
    destination = backup_dir / f'{name}.sqlite3'
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as backup_db:
        source_db.backup(backup_db)
    destination.chmod(0o600)
(backup_dir / 'database-manifest.json').write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
)
PY

EVIDENCE_ROOT="evidence"
test -d "$EVIDENCE_ROOT"
find "$EVIDENCE_ROOT" -type f -print0 | sort -z | xargs -0 -r sha256sum \
  > "$BACKUP_DIR/evidence.before.sha256"
```

只允许快进到审核提交，然后在视觉开关保持关闭的情况下完成兼容迁移和数据库完整性检查。该迁移只创建或升级 `chat_image_assets`，不会扫描历史归档或证据目录。

```bash
git merge --ff-only "$RELEASE_COMMIT"
.venv/bin/pip install -r requirements.txt

CHAT_VISION_ENABLED=false .venv/bin/python - <<'PY'
from dotenv import load_dotenv

load_dotenv('.env')
from plugins.chat_vision.store import ChatVisionStore
from plugins.violation_record.config import CONFIG

ChatVisionStore(CONFIG.chat_archive_path)
PY

.venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv('.env')
from plugins.violation_record.config import CONFIG

for database in (CONFIG.database_path, CONFIG.chat_archive_path, CONFIG.evidence_database_path):
    database = Path(database)
    if not database.is_file():
        continue
    with sqlite3.connect(database) as connection:
        result = connection.execute('PRAGMA quick_check').fetchone()[0]
    print(f'{database}: {result}')
    if result != 'ok':
        raise SystemExit(1)
PY

find "$EVIDENCE_ROOT" -type f -print0 | sort -z | xargs -0 -r sha256sum \
  > "$BACKUP_DIR/evidence.after-migration.sha256"
cmp "$BACKUP_DIR/evidence.before.sha256" "$BACKUP_DIR/evidence.after-migration.sha256"
```

确认检查通过后再启用并启动。`CHAT_VISION_IMAGE_ROOT` 必须是 `data/chat_vision/` 下的目录；服务会把受控目录逐级收紧为 `0700`，原图文件保持 `0600`，任何祖先符号链接都会导致视觉摄取安全失败而不是跟随到证据目录。

```bash
sed -i 's/^CHAT_VISION_ENABLED=.*/CHAT_VISION_ENABLED=true/' .env
grep -q '^CHAT_VISION_ENABLED=true$' .env
systemctl start qq-violation-bot.service
systemctl is-active --quiet qq-violation-bot.service
journalctl -u qq-violation-bot.service -n 100 --no-pager
```

验收时在已加入 `GROUP_CHAT_ALLOWED_GROUP_IDS` 的测试群依次发送一条纯图片消息，以及一条“@机器人 + 图片”消息。纯图片是否回复仍受聊天概率影响，但两条消息中的每张图都应独立留下永久描述；第二条应立即进入视觉聊天。检查最近状态：

```bash
.venv/bin/python - <<'PY'
import sqlite3

from dotenv import load_dotenv

load_dotenv('.env')
from plugins.violation_record.config import CONFIG

with sqlite3.connect(CONFIG.chat_archive_path) as connection:
    rows = connection.execute(
        '''SELECT group_id, message_id, ordinal, status,
                  length(description), created_at, updated_at
           FROM chat_image_assets
           ORDER BY id DESC LIMIT 20'''
    ).fetchall()
for row in rows:
    print(row)
PY
```

出现错误率上升、图片路由异常或资源占用异常时，先止损而不是回滚全部机器人。下列操作只关闭视觉摄取，文字聊天和业务功能继续运行：

```bash
cd /opt/qq-violation-bot
sed -i 's/^CHAT_VISION_ENABLED=.*/CHAT_VISION_ENABLED=false/' .env
grep -q '^CHAT_VISION_ENABLED=false$' .env
systemctl restart qq-violation-bot.service
systemctl is-active --quiet qq-violation-bot.service
```

只有兼容迁移或发布代码本身有问题时才执行完整回滚。服务停稳后切换到发布前保存的回滚分支，恢复 `.env` 和三个精确数据库目标；脚本只删除这些数据库自己的 `-wal`、`-shm` 边车文件，不访问 `evidence/`。回滚后再次执行上面的 `PRAGMA quick_check`，再启动服务。

```bash
cd /opt/qq-violation-bot
set -euo pipefail
CHANGE_ID="${CHANGE_ID:?请先执行 export CHANGE_ID=发布时生成的变更编号}"
BACKUP_DIR="backups/$CHANGE_ID"
systemctl stop qq-violation-bot.service
git switch "rollback/$CHANGE_ID"
cp -p "$BACKUP_DIR/env.before" .env

BACKUP_DIR="$BACKUP_DIR" .venv/bin/python - <<'PY'
import json
import os
import shutil
from pathlib import Path

backup_dir = Path(os.environ['BACKUP_DIR'])
manifest = json.loads((backup_dir / 'database-manifest.json').read_text(encoding='utf-8'))
for name, item in manifest.items():
    destination = Path(item['path'])
    for suffix in ('-wal', '-shm'):
        Path(f'{destination}{suffix}').unlink(missing_ok=True)
    if item['existed']:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_dir / f'{name}.sqlite3', destination)
    else:
        destination.unlink(missing_ok=True)
PY

find evidence -type f -print0 | sort -z | xargs -0 -r sha256sum \
  > "$BACKUP_DIR/evidence.after-rollback.sha256"
cmp "$BACKUP_DIR/evidence.before.sha256" "$BACKUP_DIR/evidence.after-rollback.sha256"

.venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv('.env')
from plugins.violation_record.config import CONFIG

for database in (CONFIG.database_path, CONFIG.chat_archive_path, CONFIG.evidence_database_path):
    database = Path(database)
    if not database.is_file():
        continue
    with sqlite3.connect(database) as connection:
        result = connection.execute('PRAGMA quick_check').fetchone()[0]
    print(f'{database}: {result}')
    if result != 'ok':
        raise SystemExit(1)
PY

systemctl start qq-violation-bot.service
systemctl is-active --quiet qq-violation-bot.service
```

萝卜猫只是角色名字，不自称猫或使用“喵”等猫系口癖；她喜欢花和植物，并把“反二梦女”视为自己的兴趣和自我标签。上下文会保留发送者 QQ、昵称、艾特对象和引用对象，避免把群友之间的话误当成对机器人说。查询、记录、减数、禁言等已识别业务始终优先于聊天回复。成功的聊天回复以 `RANDOM_CHAT_STICKER_PROBABILITY=0.20` 的概率附带最多一张表情包；指定首图在已决定附图时占 10%，其余图片均分 90%。表情包只保存在 `data/random_chat/stickers/incoming/`，不会提交到 GitHub，业务回复永不附图。

人物设定保存在项目根目录的 `character.md`。群聊和私聊在每次请求 AI 前都会重新读取该文件，保存后的修改会从下一条回复开始生效，无需重启；文件缺失、为空或无法按 UTF-8 读取时自动使用内置默认设定。业务隔离、对话方向、安全限制和输出规则仍由程序控制，不受角色文件影响。

`PRIVATE_CHAT_ENABLED=true` 且聊天总开关开启时，机器人只回复 `PRIVATE_CHAT_ALLOWED_USER_IDS` 指定的 QQ 私聊白名单，多个 QQ号使用英文逗号分隔，其他账号完全静默。旧的 `PRIVATE_CHAT_ALLOWED_USER_ID` 仅作兼容输入。允许账号的每条普通文字、图文或纯图片消息都会直接尝试回复，不使用群聊概率；以 `/` 开头的命令即使带图也不会绕过命令边界。私聊图片同时要求 `CHAT_VISION_ENABLED=true`：每条最多处理前 4 张，原图总字节不超过 `CHAT_VISION_MAX_BYTES`，下载和 MIME 校验复用群聊的安全下载器。纯图片全部下载/识别失败时保持静默，图文消息则只按真实文字降级回复，不伪装已经看图。

每个允许 QQ 分别使用独立上下文和串行锁，彼此不会共享内容。私聊原图只在当前请求内存中短暂存在，不写入 `data/chat_vision/images/`、`chat_image_assets`、证据目录或数据库，也不会跨重启保留；开启 `PRIVATE_MEMORY_ENABLED` 后，仅把受限长度的图片文字描述按 `user_id + message_id` 写入私聊原文层，供后续近期上下文恢复。描述与私聊原文使用同一数量/期限策略，并会被 `/记忆 清空`、到期清理和 WAL 安全清理一起删除；图片描述及其派生助手回复不进入滚动摘要、长期事实、关系状态、业务判断或违规证据，图文消息只保留用户真实输入的文字。清空、白名单变化和运行时开关会在识图、生成、逐条发送及持久化前复检；持久/非持久模式切换时会清掉进程内兼容队列，避免已清内容复活。`PRIVATE_MEMORY_ENABLED=false` 时仍只保留进程内最近 20 条双方消息及本次图片理解，重启即清空。私聊回复沿用 20% 表情包概率；紧急关闭请使用下文的 `/私聊 关`。

### 私聊持久记忆与治理

私聊持久记忆只接收同时通过聊天总开关、私聊开关和 `PRIVATE_CHAT_ALLOWED_USER_IDS` 精确白名单的部署后新消息；不会回溯、导入或重新提炼历史私聊。数据按用户 QQ号隔离，读取、写入、任务和关系状态均限定在同一用户范围。三项新开关默认均为 `false`：`PRIVATE_MEMORY_ENABLED` 控制私聊原文、摘要与长期事实的读取和新增，`RELATIONSHIP_STATE_ENABLED` 控制关系状态与未完话题更新，`MEMORY_GOVERNANCE_ENABLED` 控制超级管理员治理入口。关闭 `PRIVATE_MEMORY_ENABLED` 后不持久化新私聊内容，已有记录不会被开关自动删除。

持久层包含以下五类数据：双方原文、滚动摘要、长期事实、关系状态和未完话题。原文默认每用户最多保留 500 条且最多保留 30 天，分别由 `PRIVATE_MEMORY_MAX_MESSAGES` 和 `PRIVATE_MEMORY_RETENTION_DAYS` 调整；启动时立即清理一次，之后每 24 小时清理一次。含私聊正文的受管迁移备份和服务自动迁移备份统一放在 `backups/private_memory/` 并使用同一个保留天数：只按修改时间删除符合本服务两种精确命名规则的过期普通单链接文件，即使之后不再执行迁移，daily 任务也会清理两类过期备份；其他文件、符号链接和硬链接不会删除。备份目录必须为 `0700`，目录本身或任一祖先符号链接都会导致操作被拒绝。迁移 `--apply` 会在创建新备份前执行一次同样的清理。清理事务启用 SQLite `secure_delete` 并尝试截断 WAL checkpoint；治理清空遇到 checkpoint busy 时会持久记录可检索审计，逻辑清理仍返回成功；每日保留清理遇到 busy 时只写脱敏日志并在下一周期重试。系统不会在线自动执行 `VACUUM`。摘要、长期事实、关系状态和未完话题不使用原文的 500 条/30 天自动过期规则。

这是敏感数据功能：原文、摘要和推断事实可能包含隐私，模型提取也可能不准确。生产数据库、WAL、备份和日志目录应仅允许服务账号访问；不要把真实 QQ号、私聊内容、数据库或备份提交到仓库。管理员应先查看和预览，再凭事实依据治理，不要把模型推断当作已核实事实。

只有 NoneBot `SUPERUSERS` 可使用 `/记忆`。查看结果、写操作预览、操作码和最终结果都私聊发送给操作者，群内只显示不含内容的状态。所有写操作先生成预览；操作码绑定操作者和预览内容，10 分钟后过期且只能确认一次，确认时必须填写原因。

```text
/记忆 <QQ号|@群成员>
/记忆 关系 <QQ号|@群成员> [新状态]
/记忆 添加 <QQ号|@群成员> <内容>
/记忆 修改 <G-编号|P-编号> <内容>
/记忆 删除 <G-编号|P-编号>
/记忆 清空 <白名单QQ号>
/记忆 状态
/记忆 确认 <操作码> <原因>
/记忆 取消 <操作码>
```

`/记忆 清空` 只清理该用户的私聊原文、滚动摘要、未完话题和待处理摘要任务；长期事实需按 `P-编号` 分别删除，关系状态可通过关系命令修订。当前没有原文或全量记忆文件导出命令；授权管理员可用 `/记忆 <白名单QQ号>` 和 `/记忆 关系 <白名单QQ号>` 私下查看治理视图，灾备导出使用下述经过校验的 SQLite 在线备份。不要用未审计的临时 SQL 把私聊正文导出到公共目录。

#### 迁移、启用与回滚

迁移现有 `chat_archive.db` 前先保持 `/私聊记忆`、`/关系状态`、`/记忆治理` 为关闭，确认数据库路径，并创建权限为 `0700` 的备份目录。以下命令必须从项目根目录执行，并用 `pwd -P` 取得不含符号链接的物理绝对根路径；数据库、备份目录或任一祖先符号链接以及其他非 canonical 路径都会在 preflight 和 `--apply` 写入前被同样拒绝。生产已有 `.env` 时先由 shell 安全加载并导出其中的现有配置，使 `TARGET_GROUP_ID` 等必需配置可供迁移子进程使用，命令不会输出这些值。无 `--apply` 是只读预检；`--apply` 会清理 `backups/private_memory/` 中超过 `PRIVATE_MEMORY_RETENTION_DAYS` 的精确命名旧备份，再为现有数据库创建并校验在线备份，最后做迁移和 `quick_check`。不要删除或复制正在使用的 `-wal`/`-shm` 文件代替在线备份。

```bash
cd /opt/qq-violation-bot
PROJECT_ROOT="$(pwd -P)"
set -a
. ./.env
set +a
install -d -m 0700 "$PROJECT_ROOT/backups/private_memory"
.venv/bin/python scripts/migrate_private_memory.py \
  --database "$PROJECT_ROOT/data/chat_archive.db" \
  --backup-dir "$PROJECT_ROOT/backups/private_memory"
.venv/bin/python scripts/migrate_private_memory.py \
  --database "$PROJECT_ROOT/data/chat_archive.db" \
  --backup-dir "$PROJECT_ROOT/backups/private_memory" \
  --apply
.venv/bin/python scripts/migrate_private_memory.py \
  --database "$PROJECT_ROOT/data/chat_archive.db" \
  --backup-dir "$PROJECT_ROOT/backups/private_memory"
```

在数据库副本或预发布环境再执行一次 `--apply`，验证重复迁移仍成功、schema version 不变、原有业务/聊天记录不变、每个备份文件权限为 `0600`，并用 `PRAGMA quick_check` 验证原库和备份均为 `ok`。全新空库由服务启动时创建 schema；迁移脚本要求目标数据库已存在，不会为了预检凭空创建数据库。

迁移后先在所有新开关关闭时重启并验证既有业务、群聊、私聊和图片路径。然后逐项执行并在每一步检查 `/模块状态`：先 `/记忆治理 开`，再 `/私聊记忆 开`，最后 `/关系状态 开`；只有既有聊天总开关、私聊开关和白名单均已正确配置，白名单私聊才会进入持久路径。烟雾检查至少包括：数据库 `PRAGMA quick_check` 为 `ok`；schema version 为当前版本；非白名单私聊无读写；两个白名单测试用户互相看不到内容；一条部署后新私聊产生原文及任务；`/记忆 状态` 和查看结果只私发；预览 10 分钟有效且一次确认后不可重放；关闭 `/私聊记忆` 后新私聊不再新增持久记录。

异常时优先止损，不要先恢复数据库：`/私聊记忆 关` 停止新的私聊持久内容与摘要/事实任务，`/关系状态 关` 停止关系更新，`/记忆治理 关` 关闭治理入口，必要时 `/私聊 关` 停止整个白名单私聊入口。代码回滚只切换程序版本，不会删除迁移后 schema 或新数据；数据库恢复会把数据库整体恢复到备份时点并丢失该时点之后的聊天及记忆写入。只有确认迁移数据损坏且明确接受数据丢失时，才停服、保留故障库副本、移走该数据库自己的 `-wal`/`-shm`，恢复精确在线备份并再次执行 `PRAGMA quick_check`。二者不要混为一步。

如合规要求必须回收空闲页，应先完成在线备份，在维护窗口停服后人工执行 `VACUUM`，随后运行 `PRAGMA quick_check`；这不是每日 retention 的一部分。checkpoint busy 只表示物理 WAL 清理待重试，不表示已提交的逻辑删除失败，也不应因此重复确认同一个操作码。

成员记忆独立于随机回复概率持续收集。原始特性和历史昵称以追加式账本永久保存在服务器 SQLite 中，不再按 8 条上限淘汰；本地 JSON 镜像包含完整历史。每累计 5 条新特性会生成一次不超过 300 字的滚动摘要，聊天 AI 只读取摘要、最多 5 个近期旧称和最多 8 条尚未摘要的特性。`MEMBER_MEMORY_SUMMARY_ENABLED=false` 只关闭摘要生成，永久账本仍继续写入。真实成员记忆与镜像不会提交到 GitHub。

## 统一模型网关与提示构建

统一模型网关（LLM Gateway）只负责模型传输，不拥有业务或聊天提示词。业务意图、聊天、成员记忆、私聊摘要/关系和图片描述仍由各自模块构造独立请求；聊天模型不能直接执行违规记录、禁言、状态或减数操作。网关在进程内共享一套异步连接池，统一管理 Base URL、API Key、模型、超时、重试和连接关闭，不复制密钥或为每次调用新建长期客户端。

网关错误只按类别记录和返回，例如配置、鉴权、超时、网络、限流、服务端、客户端、空内容和契约错误；日志不写请求正文、模型响应、Authorization、图片字节或 API Key。取消信号直接传播。只对网络失败、超时、HTTP 429 和可恢复的 5xx 做有限退避重试；鉴权、普通 4xx 和响应契约错误不盲目重试。

总并发和各调用域并发分别由 `LLM_GATEWAY_TOTAL_CONCURRENCY` 与 `LLM_GATEWAY_*_CONCURRENCY` 限制。任务先取得自身通道，再进入总并发，避免排队中的聊天请求占住图片或业务容量。关闭服务时先停止接收新请求并有界等待在途调用；超时后取消剩余任务并关闭共享连接。每次调用把任务类型、模型、输入/输出/总 token、延迟、状态、重试次数和脱敏错误类别写入 `llm_usage_events`；供应商未返回 token 时保持空值，当前不估算费用，`cost_microunits` 和 `cost_currency` 保持空值。统计写入失败不会改变业务或聊天结果。

Prompt Builder 只用于聊天，不用于业务意图。固定安全、方向和输出规则位于高权限 system 消息；`character.md`、近期上下文、成员事实、关系状态、未完话题、图片描述和当前消息均作为带标签的不可信数据放入 user 消息，即使其中含“忽略规则”等文本也不能覆盖权限和业务边界。每次请求会按精确 QQ号建立临时 `speaker_ref` 目录，并让历史消息、成员事实、@、引用作者和当前发送者使用同一套引用；第一人称只归属该条消息的作者，同名不同 QQ 不合并，引用作者也不会替代当前发送者。该目录不持久化、不跨请求共享，也不会把私聊记忆带进群聊。默认字符预算如下：人设 2000、最近上下文最多 20 条/6000、成员事实 1200、关系状态 600、未完话题最多 5 条/400、图片描述 2000、当前消息 2000、最终请求 12000。超限时按确定顺序裁剪最旧上下文等可裁剪数据，同时同步裁剪无用说话者目录项；固定规则和当前消息不会被静默丢弃，预算按最终转义后的实际内容计算。

聊天联网搜索使用 Tavily，默认关闭。它只会在白名单私聊或群内明确 @ 机器人时，针对“搜一下/查一下”等明确请求或明显时效问题查询；普通随机群聊、业务意图、违规判断、禁言、状态和减数策略永远不会调用搜索。查询只取当前消息并限制为 200 字，不发送 QQ号、昵称、历史、记忆、关系状态或图片；最多读取 5 条结果、4000 字，结果作为 `<web_search_data>` 不可信 user 数据注入。搜索失败会降级为普通聊天，并约束模型不得假装已经查到实时信息。每个实例必须在自己的 `.env` 中配置 `TAVILY_API_KEY`，不得写入 Git、运行时状态或日志；基础查询会消耗 Tavily 账户额度。

私聊和群内明确 @ 的聊天允许模型在确有需要时返回 1–3 条连续消息；能一句说完时仍只发一句，不按标点机械拆分。消息间隔约 350 ms，表情包只附在最后一条；发送中途失败时停止后续发送，私聊只持久化实际发送成功的部分。普通概率随机接话始终最多一条，避免刷屏。

所有第二阶段功能默认关闭。`.env` 只提供首次启动默认值，QQ 运行时状态仍优先。模型网关总开关和对应调用域子开关必须同时开启；`/提示构建` 可独立切换。关闭某个调用域会立即让后续请求回到该模块原有传输路径；关闭 Prompt Builder 会让聊天回到原有提示组装。关系、记忆和图片描述无论使用哪条模型路径都只影响聊天表达，不能进入业务判断。

现有 `chat_archive.db` 从 schema v1 升级到 v2 时新增 `llm_usage_events`，从 v2 升级到 v3 时为私聊原文增加有界图片描述字段。两次升级都继续使用“私聊持久记忆与治理”章节的同一迁移脚本、在线备份、`PRAGMA quick_check` 和重复迁移验证流程；不要另建第二个数据库。v3 不读取、导入或回溯任何旧私聊图片，旧行的描述默认为空数组。生产发布时先让全部网关与 Prompt Builder 开关保持关闭，验证旧业务、群聊、私聊、成员记忆和图片路径，再按以下顺序灰度：

```text
/模型网关 开
/模型网关 视觉 开
/模型网关 私聊记忆 开
/模型网关 成员记忆 开
/模型网关 聊天 开
/提示构建 开
/模型网关 业务 开
```

每一步都用 `/模块状态` 确认，并至少验证一次成功调用、一次脱敏故障分类与关闭子开关后的旧路径恢复，同时确认 `llm_usage_events` 新增一条不含正文的记录。聊天阶段检查明确 @ 必回、普通消息概率不变、表情包与图片理解正常、两个私聊白名单用户不串线；业务阶段最后执行固定语料烟雾检查，覆盖新增记录、模糊成员查询、分区查询、禁言及否定禁言、确认、取消和非法 JSON，确认预览与后端校验结果不变。

异常时先关闭最窄子开关。例如视觉异常用 `/模型网关 视觉 关`，聊天异常先 `/提示构建 关`，仍异常再 `/模型网关 聊天 关`；业务异常立即 `/模型网关 业务 关`，不要关闭整个业务模块。需要停止全部新网关调用时使用 `/模型网关 关`。这些止损只切回旧代码路径，不删除使用统计、记忆或关系数据，也不需要数据库回滚。只有 schema 迁移损坏时才按前述停服、保留故障库、恢复在线备份和再次 `quick_check` 的流程处理。

`EVIDENCE_REQUIRED=false` 表示新增违规记录未引用证据图片时只提醒、不阻止现有记录流程；改为 `true` 后才要求提供证据。只有新增违规命令所引用消息中的图片会被下载并持久化，每张图片最大允许 `EVIDENCE_MAX_BYTES` 字节。查询时，每条违规记录的文字和该记录映射的全部图片会尽量通过同一条 OneBot 混合消息发送；混合消息发送失败时会回退为文字和图片分别发送。旧记录没有证据图片时仍可正常查询。

`MUTE_ENABLED=false` 默认关闭群禁言执行；启用后才会调用 OneBot/Napcat 的群管理接口。该开关不改变现有违规记录、查询和确认流程。

`DEDUCTION_POLICY_V102_ENABLED=false` 默认保留原每周减计机制。准确数据完成预演、迁移和回滚演练后才可改为 `true`；启用后只运行 v1.0.2beta 策略引擎，旧减计任务停止结算，避免重复减数。`DEDUCTION_POLICY_RULE_VERSION` 用于事件审计，生产环境应保持为发布版本 `v1.0.2beta`。

## 模块化运行时功能控制

新部署请在 `.env` 中保留以下安全默认值；示例中的群号是合成占位值，不是实际运行群号：

```dotenv
BUSINESS_ENABLED=true
CHAT_ENABLED=false
GROUP_CHAT_ENABLED=false
GROUP_CHAT_ALLOWED_GROUP_IDS=123456789
PRIVATE_CHAT_ENABLED=false
PRIVATE_CHAT_ALLOWED_USER_IDS=
PRIVATE_MEMORY_ENABLED=false
RELATIONSHIP_STATE_ENABLED=false
MEMORY_GOVERNANCE_ENABLED=false
PRIVATE_MEMORY_RETENTION_DAYS=30
PRIVATE_MEMORY_MAX_MESSAGES=500
PRIVATE_MEMORY_SHUTDOWN_TIMEOUT=10
```

`BUSINESS_ENABLED` 控制当前 `TARGET_GROUP_ID` 的违规记录、查询、导出、减数策略和全部业务提醒，也覆盖旧版自动维护与周报生成、通知和群文件上传；关闭时数据库备份与证据存储清理仍可继续。`CHAT_ENABLED` 是聊天总开关；它关闭时，群聊和私聊子功能都不能处理消息。`GROUP_CHAT_ENABLED` 与 `GROUP_CHAT_ALLOWED_GROUP_IDS` 共同控制群聊回复、消息归档和成员记忆；`PRIVATE_CHAT_ENABLED` 与 `PRIVATE_CHAT_ALLOWED_USER_IDS` 共同控制私聊回复。两个白名单都使用英文逗号分隔的正整数 QQ号，群聊白名单填群号，私聊白名单填用户 QQ号。

父开关不会删除子开关或白名单。因此，重新开启 `CHAT_ENABLED` 后会恢复原有群聊/私聊子配置；仅开启子开关不足以绕过关闭的聊天总开关。关闭 `GROUP_CHAT_ENABLED` 只暂停群聊回复、归档和成员记忆，不影响私聊；关闭 `PRIVATE_CHAT_ENABLED` 只暂停私聊，不影响群聊。业务功能和聊天功能独立：业务群只有在 `GROUP_CHAT_ALLOWED_GROUP_IDS` 中时才会聊天；聊天白名单中的其他群永远不进入业务意图判断。

`.env` 只提供首次启动默认值。首次通过 QQ 管理命令修改后，状态会原子写入 `data/runtime_features.json`，并保留上一份有效备份 `data/runtime_features.json.bak`；两者都被 Git 忽略。有效的运行时状态优先于 `.env`，如需恢复 `.env` 默认值，应在停服后按运维变更流程备份并移除这两个运行时状态文件，再启动服务。

为兼容旧部署，当环境中同时缺少 `CHAT_ENABLED` 与 `GROUP_CHAT_ENABLED` 时，会把聊天总开关和群聊子开关迁移为开启，并让 `TARGET_GROUP_ID` 继续参与群聊归档、成员记忆和回复；这避免旧环境在升级后静默停止历史功能。新部署应保留 `.env.example` 中显式的六项安全默认配置。`PRIVATE_CHAT_ENABLED` 仍可提供首次私聊默认值；旧的 `PRIVATE_CHAT_ALLOWED_USER_ID`（可用英文逗号分隔多个 QQ号）仍作为 `PRIVATE_CHAT_ALLOWED_USER_IDS` 的兼容输入，新部署和后续维护应使用 `PRIVATE_CHAT_ALLOWED_USER_IDS`。

### QQ 运维命令与恢复

只有 NoneBot `SUPERUSERS` 可执行以下命令。`/模块状态` 只显示白名单数量；使用列表命令时才向授权操作者显示相应白名单。

```text
/模块状态
/业务 开
/业务 关
/聊天 开
/聊天 关
/群聊 开
/群聊 关
/群聊群 添加 <群号>
/群聊群 删除 <群号>
/群聊群 列表
/私聊 开
/私聊 关
/私聊记忆 开
/私聊记忆 关
/关系状态 开
/关系状态 关
/记忆治理 开
/记忆治理 关
/模型网关 开
/模型网关 关
/模型网关 视觉|私聊记忆|成员记忆|聊天|业务 开|关
/提示构建 开
/提示构建 关
/联网搜索 开
/联网搜索 关
/私聊用户 添加 <QQ号>
/私聊用户 删除 <QQ号>
/私聊用户 列表
```

紧急止损时，优先使用对应的 QQ 命令，无需重启或回滚代码：`/业务 关` 停止新业务请求、v1.0.2beta 与旧版自动维护提醒，以及尚未开始的周报生成、通知和上传；每次实际外发前都会再次检查开关。`/聊天 关` 停止全部聊天入口；`/群聊 关` 只停群聊、归档和成员记忆；`/私聊 关` 只停私聊；`/私聊记忆 关`、`/关系状态 关` 和 `/记忆治理 关` 分别停止新的持久记忆、关系更新和治理入口。开关修改失败会保留旧状态并返回失败信息，不能把失败当作已生效。

联网搜索异常时先执行 `/联网搜索 关`，后续聊天立即回到不搜索路径，不需要回滚数据库。多段回复不需要数据库迁移；代码回滚后已经写入的多条私聊助手消息仍是普通私聊原文。

启用 `DEDUCTION_POLICY_V102_ENABLED=true` 时，业务关闭、QQ/OneBot 离线或发送失败造成的策略提醒会保留在持久化队列中。业务开启期间生成的旧版维护通知会在逐条外发前持久化，周报任务会在生成文件前以日期幂等键持久化；离线、API 失败或中途关闭业务均会保留失败状态，服务重启也不会按普通消息重新发送。业务重新开启且发送通道恢复后，机器人只发送一次 QQ 合并转发的“未发送业务提醒概览”：首节点给出时间范围、提醒总数、涉及成员数、消息类型计数及各失败原因数量；策略提醒按成员归组，旧版/周报项目单独归组，不会逐条以普通群消息补发。只有该合并转发成功后，对应记录才标记为已处理；发送失败时记录保留，等待后续检查重试。

未来的 OA 集中管理平台不在本次交付范围内。本次只保留单一功能控制服务作为运行状态的读写边界；未来 OA 应调用该边界或其上层 API，而不应让插件直接读写运行时 JSON 文件。

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

验证业务：在 `TARGET_GROUP_ID` 配置的群里发送 `@违规记录助手 帮助`。业务路径只处理该群中 @ 机器人的消息；其他群或未 @ 机器人的消息不会进入业务处理。验证群聊：由超级管理员开启 `CHAT_ENABLED` 与 `GROUP_CHAT_ENABLED`，并将测试群加入 `GROUP_CHAT_ALLOWED_GROUP_IDS` 后，在该群 @ 机器人；普通聊天消息仍按 `RANDOM_CHAT_PROBABILITY` 概率回复，不适合作为必回验证。

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

业务开启时，每周日 00:10 先以 `weekly:<date>` 登记当日任务，再生成 XLSX 周报，包含本周操作日志和当前各分区成员统计；启用 v1.0.2beta 后还会附加“减数策略日志”“减数待办”“通知发送历史”和“状态联动作业”。生成后会尝试上传到群文件；成功状态会跨重启阻止重复生成和发送，离线、API 失败或发送中途关闭业务会记入后续合并漏发概览。业务关闭时本次周报工作直接跳过，不生成、不发送、不上传，也不加入漏发队列。

## 双实例 Swap 安全垫

同机运行 CArroT 与 kona 前，使用仓库内的幂等脚本配置 2 GiB Swap。脚本只管理精确的 `/swapfile`、带标记的 `/etc/fstab` 段和 `/etc/sysctl.d/99-qq-bots-swap.conf`，默认将 `vm.swappiness` 设为 10：

```bash
sudo scripts/provision_swap.sh apply --size-gib 2 --swappiness 10
sudo scripts/provision_swap.sh status --swappiness 10
```

重复执行 `apply` 不会重复写入配置。只有明确执行 `remove` 才会先停用并删除这组受管状态：

```bash
sudo scripts/provision_swap.sh remove
```

Swap 只用于降低突发内存压力导致进程被系统终止的概率，不代替内存监控；若长期持续使用 Swap，应升级内存或调整实例负载。

## CArroT / kona 双实例发布

两个机器人共用按 Git SHA 生成的只读发布目录，但各自拥有独立的 `.env`、`character.md`、数据库、记忆、图片、表情包、NapCat 配置、日志和备份。CArroT 使用 `BOT_MODE=full`，kona 必须使用 `BOT_MODE=chat_only`。实例目录分别为：

```text
/opt/qq-bots/instances/carrot
/opt/qq-bots/instances/kona
```

CArroT 是候选验证实例。开发提交先通过 `scripts/deploy_carrot_candidate.sh` 的完整本地门禁，仅推到服务器的 `release/carrot-candidate`，随后只切换 CArroT。QQ 验证通过后，才把同一提交合入并推送 GitHub `main`。`main` 的 CI 通过后，在 GitHub Actions 手动运行 `Promote kona`，输入当前 `main` 的完整 40 位 SHA，并在受保护的 `kona-production` 环境中人工批准。任何 `push` 都不会自动部署 kona。

服务器把候选代码对象保存在 `/opt/qq-bots/repository.git` 裸仓库，稳定部署入口保存在 `/opt/qq-bots/bin/`。部署入口不依赖某个发布目录，因此即使当前版本健康检查失败并回滚，也不会丢失下一次部署能力。kona 晋级时先从公开 GitHub `main` 获取已批准的精确提交，再只切换 kona。

每个新发布目录都必须包含由部署工具生成的 `.release-manifest.json`，记录真实 commit、Git tree 和公开源码摘要。已有同名 SHA 目录只有在仓库 commit、清单和全部受跟踪源码完全一致且没有额外源码文件时才能复用；目录名像 SHA 但不是仓库 commit、清单缺失、源码被修改或被加塞都会拒绝部署。实例健康检查会再次验证该清单。成功切换后，实例自己的 `previous` 指针原子指向切换前版本；同 SHA 幂等重试和失败回滚都不会覆盖既有 `previous`。发布切换与旧版本清理共用同一把排他锁，清理同时保护所有实例的 `current` 和 `previous`。首次从旧式无清单 release 升级时仍须额外保留旧目录和数据库在线备份；旧目录不会因“回滚”需求而绕过 Git/清单校验。

群内模块管理与记忆治理命令必须以目标机器人的真实 @ 开头；私聊管理命令不需要 @。因此两个机器人在同一群时，只会由明确被 @ 的实例执行命令。

## NapCat 资源监控与定时重启

`qqbot-napcat-watchdog@<实例>.timer` 每 5 分钟只检查对应 `napcat@<实例>.service` systemd cgroup 内的进程。任一 QQ/Node 进程文件描述符达到 `1500`、重复打开 `/proc/<pid>/maps` 的文件描述符达到 `1000`、任一 Xvfb 进程文件描述符达到 `220`，或最近 10 分钟出现 `Maximum number of clients reached` 时触发恢复。反向 WebSocket 会按 carrot=6199、kona=6299 分别检查，连续失败两次才触发恢复。每个实例有独立状态与 30 分钟重启冷却期。

`qqbot-napcat-daily-restart@<实例>.timer` 每天 04:10 请求一次计划重启。触发恢复时只重启目标 `napcat@<实例>.service`，不会重启另一个 QQ 或机器人服务。重启后最多等待 90 秒，检查对应 NapCat、机器人服务、反向 WebSocket 和资源指标是否恢复。

查看两个 timer 的加载、启用和运行状态：

```bash
systemctl status qqbot-napcat-watchdog@carrot.timer qqbot-napcat-daily-restart@carrot.timer
```

只采集当前指标和决策，不执行重启：

```bash
cd /opt/qq-violation-bot
.venv/bin/python scripts/napcat_watchdog.py --instance carrot --check-only
```

查看当天 watchdog 执行记录：

```bash
journalctl -u qqbot-napcat-watchdog@carrot.service --since today --no-pager
```

回滚自动恢复策略时，立即停止并禁用两个 timer；此命令不会停止机器人或 NapCat：

```bash
systemctl disable --now qqbot-napcat-watchdog@carrot.timer qqbot-napcat-daily-restart@carrot.timer
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

- 业务指令不回复：确认消息来自 `TARGET_GROUP_ID`，并且 @ 的是 `BOT_SELF_ID` 对应机器人；其他群或未 @ 机器人的消息不会进入业务路径。
- 聊天不回复：确认 `CHAT_ENABLED`、`GROUP_CHAT_ENABLED` 均已开启且群号在 `GROUP_CHAT_ALLOWED_GROUP_IDS` 中；明确 @ 机器人应进入聊天回复，普通消息仅按 `RANDOM_CHAT_PROBABILITY` 概率回复。
- 提示缺少群聊：业务指令必须写 `蜂巢 / 蜂窝 / 蜂箱`。
- AI 解析失败：检查 `AI_API_KEY`、`AI_BASE_URL`、服务器网络。
- 处理人匹配不到：先在允许群里 @ 机器人触发群成员同步，或用 `scripts/manage_admin.py list/add` 检查管理员昵称和别名。
- QQ昵称查不到：先用 `QQ昵称（QQ号）` 新增一条记录，或后台维护 members 表。
- 已锁定不能新增：先查询确认状态，再用解锁指令或后台修改。
- 禁言失败：确认机器人 QQ 是群管理员，目标在本群内，且目标不是群主或权限高于机器人。
