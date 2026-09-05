# 实例部署、健康检查与公开发布门禁

适用工具版本：`qqbot-ops-2026.09.05.1`。此批次修改运维脚本，不修改业务分词、语意理解、词库、线上账号配置或实际数据库路径。生产备份、稳定工具目录更新和候选部署由维护者统一执行。

## 稳定工具目录

`deploy_instance.py`、`instance_health.py`、`napcat_watchdog.py`、`check_public_tree.py` 与 `ops_runtime.py` 必须作为同一批工具更新；不可只替换其中一个文件。共享模块只依赖 Python 标准库，脚本既支持源码树执行，也支持 `/opt/qq-bots/bin/` 执行。健康检查不从稳定目录推测插件根，而是在验证 release 源码后直接装载该 release 的纯状态解析器，不触发插件注册。

更新前保留原工具副本和摘要；暂停并等待既有部署/巡检操作结束，再在维护窗口替换整组文件。工具不自行改写稳定 bin。以下为只读核对示例，`<40位SHA>` 需要替换为批准的候选提交：

```sh
python3 -B /opt/qq-bots/bin/deploy_instance.py --version
python3 -B /opt/qq-bots/bin/instance_health.py --version
python3 -B /opt/qq-bots/bin/napcat_watchdog.py --version
python3 -B /opt/qq-bots/bin/instance_health.py \
  --instance carrot --sha <40位SHA> --root /opt/qq-bots \
  --repo /opt/qq-bots/repository.git \
  --expected-ops-version qqbot-ops-2026.09.05.1
python3 -B /opt/qq-bots/bin/napcat_watchdog.py --instance carrot --check-only
```

健康检查成功输出 JSON：实例、发布 SHA、工具版本、执行脚本/共享模块的 SHA256，以及只读 API 的身份、online、good 检查结果。运维版本参数可拒绝调用版本不匹配的健康脚本；脚本摘要用于与批准的工具副本核对。

## 健康检查范围

必须同时满足：

1. `current` 指向请求的安全 release，Git commit/tree、全部跟踪源码内容与权限符合 manifest。
2. systemd 为 active，MainPID 属于目标 Bot cgroup；运行进程的实际工作目录等于目标 release，命令入口为 `bot.py`。仅改变软链而未重启不能通过。
3. 精确的 `127.0.0.1:6199`（carrot）或 `127.0.0.1:6299`（kona）处于 Bot 进程拥有的 LISTEN；同一环回 TCP 连接的两端分别属于该 Bot 和对应 NapCat 的 cgroup。相邻端口、无监听、其他实例/进程的连接均不能通过。
4. 在 NapCat HTTP API 调用且仅调用 `get_login_info`、`get_status`，核对 `BOT_SELF_ID`，并要求 `online=true`、`good=true`。
5. 使用运行版本相同的状态解析器核对主状态/备份状态及必要环境约束，包括 kona 的 chat-only 边界和穷鬼模式供应商配置。
6. v2 release 的解释器、虚拟环境入口、`pip check` 与构建时版本摘要一致。

HTTP 默认地址为 carrot `http://127.0.0.1:6201`、kona `http://127.0.0.1:6301`，令牌读取实例 `.env` 的 `NAPCAT_ACCESS_TOKEN`。可选 `ONEBOT_HTTP_URL` 只接受带显式端口的 `http://127.0.0.1` origin，禁止远端地址、用户信息、路径、查询参数和重定向跳转；不使用系统代理。请求超时、无效回包、身份不符或未登录均失败。日志和返回结果不包含令牌、QQ 号、昵称或响应正文。

历史 traceback 不再永久锁死本次 invocation 的健康状态。该探针证明进程/版本、连接、QQ 登录身份和选定配置边界，不证明每种业务、模型回复或管理员已读；这些行为应由对应回归和授权的验收覆盖。

部署调用的单次健康子进程最多 20 秒，总探针预算 60 秒，保留最后一项脱敏错误。失败自动回滚后对旧 SHA 重新运行健康检查并分别报告回滚成功或仍失败。首次部署没有旧版本时，主 CLI 停止失败候选并移除 `current`，不会对不存在的旧版本重复 restart。最近失败的结构化记录位于实例 `.deployment-result.json`，包含请求/旧 SHA、失败类别、回滚结果、工具摘要及时间；原异常作为异常链保留。代码回滚不自动覆盖业务库、聊天库或发布后新写入的数据，涉及 schema 的变更必须提前准备真实路径的备份和兼容性验证。

watchdog 的 `--check-only` 不创建目录/锁文件，不写入或清零失败计数，不更新重启时间，不触发重启。它是无锁的时点观察，可能与正在执行的 timer 同时看到不同瞬间状态；常规运行仍保留互斥、连续失败阈值和冷却。

## 依赖与虚拟环境

构建先验证并移动纯源码到最终 `releases/<SHA>`，再在该最终路径创建 `.venv`。全过程持有部署锁，健康通过前不切换 `current`；构建失败清理本次新建的候选目录。已有发布目录不原地重建环境，避免影响共用同一 release 的实例。

如果根目录存在 `requirements.lock`，仅接受精确的 `package==version` 条目并优先安装该文件。没有 lock 时保留 `requirements.txt` 兼容路径，但不能据此声称依赖解析可复现。工具不自动升级 pip，也不选择新的版本；精确清单由现场已验证版本生成。安装使用候选的 `python -m pip`，随后验证依赖、解释器前缀、所有 Python console entrypoint 的绝对解释器路径与 activate 路径。

v2 manifest 增加 `build` 元数据：Python 版本/实现、解释器文件 SHA256、规范化 `pip freeze --all` SHA256、所用 requirements 文件及其 SHA256、运维工具版本和代码摘要。复用候选、部署健康检查会对比环境摘要，检测安装集合或解释器漂移。它不等同于每个 wheel/已安装源码的完整制品签名；更强的二进制重现需要受控 wheel 制品与摘要。

旧 v1 release 的源码和实际运行状态仍可被检查，用于回滚现存版本；由于缺少环境构建元数据，不能声称旧版依赖已完成 v2 级别验证。`prepare_release` 拒绝原地复用这类旧环境，要求新 SHA 构建。线上已知旧 console entrypoint 问题不会被工具偷偷原地重写。

## 公开扫描

```sh
python3 -B scripts/check_public_tree.py
python3 -B scripts/check_public_tree.py --range <发布基线SHA>..HEAD
# 需要审查所有本地可达历史时才使用：
python3 -B scripts/check_public_tree.py --history
# 稳定 bin 中运行时显式指定待发布工作树：
python3 -B /opt/qq-bots/bin/check_public_tree.py --repo <工作树绝对路径> --range <基线SHA>..HEAD
```

默认检查当前跟踪工作树，包含强制加入索引的文件；`--range` 还检查范围内每个提交的树，因此短暂加入后又删除的敏感文件仍会失败。当前跟踪符号链接直接拒绝且不跟随读取。文件名用 NUL 分隔读取，不依赖换行分割。

路径层拒绝生产 `.env`/`.env.*`、实例和备份目录、运行数据、SQLite、导出、日志及规则 generation，只例外允许 `.env.example` 和数据目录内的说明/忽略占位文件。内容层除既有 token 形状外，检查已知敏感键的非占位字面量赋值；不依赖 CI 存在私有 `.env`，对 JSON ID 列表也检查单个元素。源码中的变量/调用表达式与明确的文档/合成测试占位值分开处理，既有少量数值测试示例按精确文件和字面量记录例外，不能用于任意配置文件。扫描结果只输出路径和类别/键名，不输出命中值。私有环境提供的多值 ID 会拆分为单项扫描。

此门禁针对当前项目已知配置和运行文件，不是对所有可能秘密编码的通用证明。真实账号、完整词库和运行数据仍应只保存在实例私有目录；不应扩大白名单来放行生产材料。

## 本地验收

新增回归先复现旧版本的四项失败：check-only 写状态、回滚不验旧版本、公开配置遗漏、稳定 bin 导入失败；再实现修复。当前专项回归覆盖真实本地空依赖 venv 构建/入口、环境摘要漂移、回滚结果与首次失败停止、严格 socket/进程/身份、HTTP 路由与脱敏、零写入检查、当前/提交范围扫描及既有部署流水线约束。网络类测试均使用假 HTTP 对象和模拟系统命令；没有向 QQ 发送消息，也没有连接生产或安装远端依赖。
