# CArroT / kona 双实例部署与发布设计

## 1. 背景与目标

在现有服务器上运行两个相互独立的 QQ 机器人实例：

- `CArroT`：现有机器人，保留完整业务与聊天能力，继续作为开发验收和灰度实例；
- `kona`：新增纯聊天机器人，保留除违规业务外的聊天、私聊、图片理解、成员记忆、关系状态、记忆治理、Prompt Builder、表情包和运行时开关。

两个实例使用同一仓库、同一代码版本体系，但不得共享配置、密钥、QQ 登录状态、数据库、记忆、图片、表情包、日志、备份或运行时开关。功能先部署到 CArroT，由用户在 QQ 中验收；验收通过后才进入 GitHub `main`，再经手动确认推广同一提交到 kona。

## 2. 非目标

- 不把现有代码改造成一个进程内承载多个租户；
- 不在 CArroT 与 kona 之间同步聊天记录、成员记忆、私聊记忆或关系状态；
- 不把 CArroT 的表情包、数据库或运行产物迁移给 kona；
- 不允许 kona 通过运行时命令意外开启违规业务；
- 不在 Git、GitHub Actions 日志或部署产物中保存 QQ 登录凭据、API Key、聊天内容和数据库。

## 3. 总体架构

### 3.1 共享不可变代码，分离实例状态

服务器使用按 Git 提交号保存的不可变发布目录，并为两个实例维护独立的当前版本指针：

```text
/opt/qq-bots/
├── releases/
│   ├── <git-sha-a>/
│   └── <git-sha-b>/
└── instances/
    ├── carrot/
    │   ├── current -> ../../releases/<git-sha>
    │   ├── .env
    │   ├── character.md
    │   ├── data/
    │   ├── evidence/
    │   ├── backups/
    │   ├── exports/
    │   └── logs/
    └── kona/
        ├── current -> ../../releases/<git-sha>
        ├── .env
        ├── character.md
        ├── data/
        ├── evidence/
        ├── backups/
        ├── exports/
        └── logs/
```

每个发布目录包含与该提交匹配的 Python 环境，避免 CArroT 测试新依赖时提前改变 kona。服务器仅保留当前版本、上一回滚版本和必要的候选版本；删除旧发布目录前必须确认没有实例指向它。

代码新增显式 `BOT_INSTANCE_ROOT`。配置、`character.md` 和所有运行路径从实例根目录解析，不再隐式依赖代码仓库根目录。未配置该变量时保持当前单实例行为，保证现有开发和测试兼容。

### 3.2 进程与权限隔离

两个机器人分别使用独立 systemd 服务和低权限 Linux 账号：

- `qqbot@carrot.service`
- `qqbot@kona.service`
- `napcat-carrot.service`
- `napcat-kona.service`

NapCat 使用不同的 `HOME`、`XDG_CONFIG_HOME`、QQ 登录目录、Xvfb 会话、访问 Token 和反向 WebSocket 目标。建议端口：

- CArroT NoneBot：`127.0.0.1:6199`
- kona NoneBot：`127.0.0.1:6299`

所有机器人端口只监听回环地址，不新增公网防火墙端口。实例 `.env`、NapCat Token 和 API Key 权限为 `0600`；实例目录仅对应运行账号和运维 root 可访问。

## 4. 实例模式与业务硬边界

代码增加实例级能力配置：

```env
BOT_MODE=full       # CArroT
BOT_MODE=chat_only  # kona
```

`BOT_MODE=chat_only` 是不可由 QQ 运行时命令突破的能力上限：

- 业务路由始终返回不可用，不调用业务意图模型和业务服务；
- 不启动违规业务数据库初始化、业务 scheduler、提醒、周报和业务 outbox；
- `/业务 开` 和 `/模型网关 业务 开` 明确拒绝并说明当前为纯聊天实例；
- `/模块状态` 显示“业务功能：不可用（纯聊天实例）”；
- 不要求 kona 配置业务目标群；群聊候选只来自独立聊天群白名单；
- 其他聊天与记忆功能按各自运行时开关工作。

默认 `BOT_MODE=full`，CArroT 和旧部署行为保持不变。该边界由配置、路由、scheduler 和命令测试共同保证，不能只依赖 `BUSINESS_ENABLED=false` 的可变软开关。

## 5. 数据、记忆与素材隔离

所有运行路径必须落在各自 `BOT_INSTANCE_ROOT` 下。特别包括：

- `data/chat_archive.db`：群聊归档、私聊记忆、关系状态和 LLM 使用统计；
- `data/member_memory/`：群成员账本与摘要镜像；
- `data/chat_vision/`：图片原图与描述；
- `data/runtime_features.json` 及备份；
- `data/random_chat/stickers/incoming/`：实例专属表情包；
- 违规数据库、证据、导出、日志和备份目录。

CArroT 的现有运行数据在停服、在线备份和校验后迁入 `instances/carrot/`。kona 从空数据库和空表情包目录初始化，只导入用户提供的 kona `.env`、`character.md` 与必要配置。两套数据库不得通过符号链接、硬链接或共享环境变量指向同一文件。

两个机器人位于同一群时，会各自在自己的数据库中归档该群消息并生成自己的记忆。这是两份独立观察结果，不是共享记忆。明确 @ 某个机器人时只由被 @ 的机器人必答；如果两个实例都对同一群开启随机接话，则二者可能各自命中概率，因此共享群应按实例分别设置概率和白名单。

## 6. 超级管理员命令隔离

两个实例均将用户指定的两名 QQ 配置为超级管理员。为防止同群中的一条管理命令被两个实例同时执行：

- 群内模块管理和记忆治理命令必须真实 @ 当前实例；
- @ 另一个机器人、未 @、同时 @ 多个机器人或伪造纯文本 @ 均不执行；
- 私聊管理命令保持现有用法，因为私聊会话天然指向一个机器人；
- 鉴权仍先于状态读取、数据库访问和任何写操作；
- 群内回执不得泄露白名单、记忆正文、操作码或密钥。

测试至少覆盖两个不同 `self_id` 收到同一群事件时只有被 @ 实例执行，以及两名超级管理员均能在各自目标实例中操作。

## 7. Swap 与资源边界

当前服务器约 1.6 GiB 内存、无 Swap；现有 NapCat 约 396 MiB，机器人约 80 MiB。第二套 NapCat 和机器人会显著降低峰值余量。

部署前创建 2 GiB `/swapfile`：

- 创建前检查磁盘、现有 Swap、目标文件和 `/etc/fstab`，重复执行不得产生重复项；
- 文件权限 `0600`，使用 `mkswap` 和 `swapon` 启用；
- 持久配置写入 `/etc/fstab`；
- 设置 `vm.swappiness=10`，仅在内存紧张时使用；
- 验证 `swapon --show`、`free -h`、现有 CArroT 与 OneBot 连接；
- 回滚时先 `swapoff`，再移除精确的 sysctl/fstab 管理项和 `/swapfile`。

Swap 是 OOM 安全气囊，不替代真实内存。若持续使用 Swap、出现明显延迟或可用内存长期过低，应升级至至少 4 GiB 内存。

## 8. CArroT 灰度与 kona 推广流程

### 8.1 CArroT 候选发布

用户保持现有体验：功能完成后先给 CArroT 测试，确认前不推 GitHub。底层流程为：

1. 在本地功能分支形成可回滚 Git 提交；
2. 完成相关单元测试、编译、差异和隐私检查；
3. 将精确提交只推到服务器候选分支，不推 GitHub；
4. 服务器创建不可变发布目录并校验提交；
5. 原子切换 `instances/carrot/current`，重启 CArroT；
6. 验证 systemd、端口、CArroT QQ号的 OneBot 连接、插件启动和当前 SHA；
7. 用户在 QQ 中做功能验收；
8. 失败时仅将 CArroT 指针切回上一发布并重启，kona 不受影响。

禁止直接 SSH 修改 CArroT 当前发布目录或保留未提交代码。这样即使 GitHub 尚未接收候选功能，服务器版本仍可审计和回滚。

### 8.2 GitHub 与 kona 稳定发布

CArroT 验收通过后：

1. 将已验收功能分支快进合并到 GitHub `main`；
2. GitHub CI 对精确 `main` SHA 运行完整单元测试、编译、公开仓库当前树及历史扫描；
3. CI 通过后显示“可推广到 kona”，但不自动部署；
4. 用户手动触发 kona 部署并确认目标 SHA；
5. 服务器为 kona 复用同一个不可变发布目录，切换 `instances/kona/current`；
6. 重启并检查 kona systemd、端口、QQ号 OneBot 连接和当前 SHA；
7. 失败时 kona 自动恢复自身上一指针，CArroT 保持不变；
8. 成功后状态页/命令显示 CArroT 与 kona 的精确提交。正常推广完成时二者必须一致。

如果 GitHub `main` 在 CArroT 验收期间产生其他提交，不能把未经 CArroT 测试的新 SHA直接推广给 kona；必须先将目标 SHA重新部署到 CArroT 验证。

### 8.3 数据库迁移发布

需要 schema 迁移的版本逐实例处理：

- CArroT 候选部署前先对 CArroT 数据库执行现有预检、在线备份、迁移和 `quick_check`；
- CArroT 验收包括迁移后读写和回滚演练；
- kona 推广时重新对 kona 数据库独立备份和迁移；
- 代码回滚只切换发布指针；不兼容的数据库回滚必须停服并恢复对应实例自己的备份；
- 两个实例的备份不得交叉使用。

## 9. CI/CD 安全边界

GitHub 工作流只保存受限部署密钥或调用受限服务端部署入口，不保存实例 `.env`、QQ 密码、NapCat Token、API Key 或数据库。部署入口只接受：

- 允许的实例名 `carrot` 或 `kona`；
- 仓库中存在的完整 Git SHA；
- 已通过对应门禁的发布阶段。

服务端部署脚本拒绝未提交工作树、非允许实例、宽泛路径、符号链接实例根、正在被另一个部署占用的锁和缺少测试证明的 kona SHA。日志只记录实例、SHA、阶段、耗时和错误类别。

## 10. 初始部署顺序

1. 备份并核验当前 CArroT 代码、配置、数据库、记忆和 NapCat 登录目录；
2. 创建并验证 2 GiB Swap；
3. 实现并测试 `BOT_INSTANCE_ROOT`、`BOT_MODE=chat_only` 和群管理命令 @ 隔离；
4. 创建发布目录、实例目录、低权限账号和 systemd 模板；
5. 迁移 CArroT，验证功能、运行数据、QQ在线和回滚；
6. 创建空的 kona 实例目录，写入用户提供的私有配置和 `character.md`；
7. 创建独立 kona NapCat，提供二维码登录并验证正确 QQ号；
8. 保持 kona 业务硬关闭，逐项开启聊天、群聊白名单、私聊白名单、图片、记忆、关系、治理、Gateway 和 Prompt Builder；
9. 验证两个机器人同群时消息归档不串、记忆不串、群管理命令只命中被 @ 实例；
10. 建立 CArroT 候选部署和 GitHub CI / kona 手动推广工作流。

## 11. 验收标准

- CArroT 与 kona 能同时登录各自 QQ，OneBot 连接稳定；
- 两个实例代码可相同或由 CArroT 暂时领先，推广完成后 SHA 完全一致；
- 任一实例写入消息、成员记忆、私聊、图片、运行时状态或表情包时，另一实例目录哈希和数据库水位不变化；
- kona 无法通过 QQ 命令、环境默认或 Gateway 子开关开启业务；
- 同群管理命令只有真实被 @ 的实例执行；
- 两名超级管理员均可管理目标实例，不能借一台机器人修改另一台状态；
- CArroT 候选失败可单独回滚，kona 保持在线和原 SHA；
- kona 推广必须显式确认且只接受已通过 CI、已在 CArroT 验收的 SHA；
- 两套 API Key、QQ 登录数据、聊天数据和记忆均不进入 Git；
- Swap 持久启用且无重复配置，服务器重启后两个实例可恢复；
- 完整测试、编译、公开仓库扫描、systemd、端口和 OneBot 健康检查均真实返回成功。

