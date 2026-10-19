# OpenCloudOS 9 / 1GB 单机场景部署准备

本方案用于单场、低并发试点，未经过持续真实面试负载验证。保留 Pipecat 音频帧、腾讯云 ASR、真实模型接口和飞书登录，不运行本地大模型。应用与 PostgreSQL 使用原生进程，避免另外安装容器引擎。现有宝塔面板保留。

## 准备阶段的边界

- 应用只监听 `127.0.0.1:8000`，单 Uvicorn worker；数据库只监听 `127.0.0.1:5432`。
- 配置为 production，开发身份入口关闭；尚未填入飞书身份时不能以预览身份进入后台。
- 不修改 DNS、云防火墙、宝塔站点或证书；不迁移本地候选人、面试记录及密钥；不调用付费 ASR/LLM。
- 服务使用独立无登录账号。初始数据库口令和会话密钥在服务器生成，只保存在 root 可读的配置文件中。
- Nginx 配置仅提供模板。HTTPS 和正式登录未验收前，不标记为正式上线。

## 执行准备

适用于 OpenCloudOS 9 上尚无业务 PostgreSQL 集群的机器。需要 root 和软件仓库 / GitHub / PyPI 网络访问；脚本遇到已有集群、已占用 8000 端口或已有当前发布时会停止，避免覆盖。

先将本仓库检出到指定、完整 SHA。阅读 `deploy/linux/prepare-opencloudos.sh` 后运行：

```bash
cd /path/to/clean/interview-copilot
bash deploy/linux/prepare-opencloudos.sh "$(git rev-parse HEAD)"
```

脚本安装 PostgreSQL / Python 包管理器 / rsync，创建独立发布目录和虚拟环境，安装包括 voice extra 的依赖并运行 `pip check`。保留完整仓库目录，因此 Obsidian 系统文档源文件也在发布内。解析出的 Linux 依赖版本保存在发布目录 `requirements-linux.lock`，不自动回写工程仓库。

脚本生成环境文件、初始化专属数据库、启动 systemd 服务、检查生产认证、执行一次本地备份和独立临时库恢复演练，最后启用每日备份。重复执行不覆盖现有密钥；部分失败后先查看已完成步骤，不能删除数据目录后盲目重跑。

## 路径与运维

| 内容 | 服务器路径 |
|---|---|
| 不可变代码发布 / 依赖 | `/opt/interview-copilot/releases/<40位SHA>/` |
| 当前发布链接 | `/opt/interview-copilot/current` |
| 生产配置（root-only） | `/etc/interview-copilot/production.env` |
| 录音 / 知识库 | `/var/lib/interview-copilot/recordings` / `knowledge` |
| PostgreSQL 集群 | `/var/lib/pgsql/data` |
| 本地备份 | `/var/backups/interview-copilot/<UTC时间>/` |

```bash
systemctl status interview-copilot --no-pager
systemctl show interview-copilot -p MemoryCurrent -p NRestarts
journalctl -u interview-copilot -n 80 --no-pager
python3 /opt/interview-copilot/current/deploy/linux/manage.py smoke
python3 /opt/interview-copilot/current/deploy/linux/manage.py status
systemctl list-timers interview-copilot-backup.timer
```

日志可能包含业务标识；不要把完整日志、环境文件或数据库提交 GitHub。配置检查只展示字段名和有无，不展示密钥。应用 `MemoryHigh=300M`、`MemoryMax=420M`；限额是防止无界占用的运行约束，不是稳定性证明。超限会中断该进程与当前连接，应检查 `NRestarts` 和 journal 中的 OOM 记录。1GB 试点前必须验证长面试、报告生成和备份期间的资源占用；不要同时启动多个 worker。

## 备份和恢复

每天约 04:10（服务器本地时区）执行 `pg_dump` 自定义格式备份，并用 rsync 的 link-dest 保存录音和知识库快照。未变化文件通过备份间硬链接复用，避免每天完整复制全部 WAV。失败快照不成为 `latest`；不自动删除任何旧备份。剩余磁盘不足 2GiB 时备份明确失败，需要管理员处理容量。

```bash
systemctl start interview-copilot-backup.service
python3 /opt/interview-copilot/current/deploy/linux/manage.py verify-backup
```

恢复验证创建随机名称的独立临时数据库，只删除该次验证新建的临时库，不覆盖应用数据库。验证结果写入快照 manifest。数据库 dump 是一致快照；录音 / 文件复制与数据库并非同一事务，正在录音的 WAV 可能不完整，应在面试结束后再备份。配置与密钥不进入此备份，应另行受控保存。该方案没有异地备份、自动容量告警或自动保留期清理；本机损坏时本机备份也可能一起丢失。历史备份同样包含个人数据，后续清理须同时管理备份的保留期。

## 更新与回滚

初始脚本不是自动升级工具，不会替换已有发布。后续发布应在无正在进行面试时操作：先备份并验证恢复，保留当前 SHA；在新的发布目录中安装并检查依赖，停止应用后切换 `current`，启动并执行 smoke。失败时切回原链接并再次启动。代码回滚不等于数据库回滚；现有 `create_all` / 轻量 ALTER 机制没有可逆迁移，恢复数据库和文件需要停写、核对备份及单独确认，禁止在生产库直接执行临时验证库的步骤。

## 正式开放前剩余配置

1. 通过受控 SSH / 服务器本地方式填写飞书、DeepSeek 和腾讯云密钥，核对实际模型名称与可用权限。不要把真实密钥粘贴到自动化助手命令中，因为云端会保存命令内容。
2. 设置管理员和 HR open_id，并在飞书应用中配置正式回调 URI、入口和可用范围。
3. 确认本地数据与 Obsidian 内容是否迁移。迁移前使用 SQLite backup API 产生一致备份，不直接拷贝仍在写入的 db/WAL；迁移路径与录音相对引用需验证。
4. 域名 A 记录指向目标 IP，完成证书申请与自动续期；使用宝塔管理的 Nginx 或系统 Nginx 中的一套，检查模板中的证书路径、WebSocket Upgrade 与 3600 秒空闲超时。云入口只按需要开放 80/443；数据库与 8000 不直接公开。
5. 展示真实 ICP 备案号并核对服务信息，然后使用合成样本完成 HTTPS / 飞书 / 麦克风 / 腾讯云流式识别 / 模型 / 报告测试。
6. 实测峰值内存、连续面试、服务重启、跨浏览器权限和备份恢复。真实候选人数据上线前仍需完成告知、权限、加密存储及保留期操作。

参考：[OpenCloudOS PostgreSQL 指南](https://docs.opencloudos.org/en/OCS/Typical_Application_Deployment/PostgreSQL_guide/)、[PostgreSQL 资源参数](https://www.postgresql.org/docs/15/runtime-config-resource.html)、[Nginx WebSocket 代理](https://nginx.org/en/docs/http/websocket.html)。
