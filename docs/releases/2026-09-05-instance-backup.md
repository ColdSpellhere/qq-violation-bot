# 独立实例备份

`scripts/instance_backup.py` 只使用 Python 标准库，不加载或初始化业务插件。`state` 备份每六小时执行，保留最近 28 份；`full` 每天执行，保留最近 7 份。两个实例独立运行。此策略提供本机恢复点，不能代替独立主机或云存储灾备。

脚本扫描实例数据中的 SQLite 文件，并解析 `.env` 的实际 `DATABASE_URL`，所以外置在旧代码目录的业务库也会备份。清单记录源路径、设备/inode、时间、SHA-256、表行数和当前发布路径。每个 SQLite 使用 online backup，随后检查完整性；配置和数据文件存入私有压缩包，逐文件比对校验值。成功后才原子发布为完成目录。

`state` 包含数据库、实例 `.env`、角色和文本配置，明确不含图片等媒体。`full` 增加实例数据目录内所有普通文件、证据和导出。实例外 NapCat 配置须通过 `--extra-dir` 显式纳入对应 systemd 服务；不要把整个 QQ 安装或正在写入的缓存当作配置备份。源路径有符号链接时失败并报告，避免静默遗漏。在线备份只保证每个数据库的一致性；跨数据库变更前仍应停止实例，制作最终一致快照。

```sh
python3 /opt/qq-bots/bin/instance_backup.py --instance kona --mode full --extra-dir /opt/qq-bots/instances/kona/napcat/workdir/config
python3 /opt/qq-bots/bin/instance_backup.py --verify /absolute/path/to/completed-snapshot
```

备份目录权限 0700，数据库、压缩包与清单权限 0600。`--prune` 仅处理 `backups/managed-v1/<实例>` 下格式及清单均匹配的本工具已完成目录；删除前先验证至少两个保留恢复点。旧业务备份、审计快照及未完成目录不在清理范围内。普通业务启动产生的既有备份仍保留，未修改受保护的业务初始化代码。

恢复时先停止目标实例，在隔离目录验证并提取备份，将每个数据库放回清单中的实际源路径，不能将旧目录在用库误恢复到实例目录的陈旧副本。核对配置、文件权限和代码/schema兼容后启动，验证预期 QQ 身份、登录、反向 WebSocket、插件及自然消息处理。恢复覆盖在用数据需明确确认恢复点，不能由代码回滚自动覆盖发布后的新数据。
