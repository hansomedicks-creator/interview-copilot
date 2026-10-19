# 服务器准备验收记录 · 2026-10-19

阶段：服务器本机准备，尚未正式开放。仅记录本次实际执行结果，不代表真实面试负载或模型质量验收。

## 版本与环境

- 应用发布：`e169b96ab67a3e32446b843ba0f7e2d7b93ed03e`。
- 独立代理准备文件：`8f68d1c261090cb69cf7180ff90cf8784fcf74d6`。
- 系统：OpenCloudOS 9.6 / x86_64，2 核，系统报告内存 940MiB，40GB 磁盘，已有 1GB swap。
- Python 3.11.6，PostgreSQL 15.19，Pipecat 1.5.0；安装后的 `pip check` 通过。
- 原有宝塔面板保留；没有升级套餐、重装系统或关闭主机保护。

## 已核实的结果

| 核查项 | 实际结果 |
|---|---|
| 服务器准备脚本 | 2026-10-19 10:20:55–10:22:03（服务器 CST），退出码 0 |
| 服务器独立验收命令 | 10:25:53–10:26:28，退出码 0 |
| 应用启动 | `interview-copilot.service` 为 active |
| 自启动设置 | 应用、PostgreSQL、备份 timer 为 enabled；没有重启整台主机验证 |
| 应用监听 | `127.0.0.1:8000` |
| 数据库监听 | `127.0.0.1:5432` |
| 匿名访问业务 API | `/api/v1/admin/jobs` 返回 401 |
| 开发预览身份入口 | `/api/v1/auth/dev-login` 返回 404，auth/status 显示不可用 |
| 音频依赖 | capabilities 确认 Pipecat 已安装；没有删除 voice 功能来节省内存 |
| 招聘决定边界 | capabilities 的自动招聘决定、自动变更阶段均为 false |
| 配置文件 | `/etc/interview-copilot/production.env` 为 `600 root root` |
| 初始配置 | 本机生成数据库口令和会话密钥；飞书、模型、ASR 密钥尚未迁移 |
| 本地备份 | 创建数据库、录音目录和知识库目录备份，归档校验通过 |
| 恢复演练 | 在独立临时库恢复成功，校验 26 张表；应用数据库未替换 |
| 恢复 manifest | `restore_drill_performed=true`，`off_server_backup=false` |
| 代理准备与重启验证 | 11:10:10–11:12:21，退出码 0；Caddy 完整配置 validate 和本机代理 smoke 通过 |
| 应用重启后 | active，`NRestarts=0`，健康和生产认证 smoke 通过 |
| 新建业务数据库 | 候选人 0，面试轮次 0；未迁移本地材料 |
| 公网入口状态 | 代理 service 为 disabled；80/443 未监听，临时代理 18080 已关闭 |
| 下一次备份 | 当次 timer 显示 2026-10-20 04:12:52 CST；尚未等待该定时事件实际触发 |

备份样本是新建的空业务数据库和空业务材料目录；不能把此演练描述为真实候选人材料的完整迁移演练。录音与文件热备份并非和数据库同一事务，活动 WAV 可能不完整。

## 测试记录

- Windows 开发环境：183 项 Python 回归通过；前端权重编辑 / 报告渲染 22 项断言和候选人分析渲染 18 项检查通过。
- 服务器 Linux 环境：`183 passed, 2 warnings in 23.94s`；使用隔离的 test/mock 环境和合成数据，未调用收费 ASR / LLM。
- 两条警告是现有 Starlette TestClient / httpx 依赖弃用和 Python audioop 弃用。
- 部署 smoke 独立检查了页面、前端脚本、健康接口、生产认证、Pipecat 可用性及自动决定限制。
- Linux 回归结束后应用仍为 active，`NRestarts=0`；没有观察到 swap 使用。

资源仅为当次观察：初次 smoke 后应用 cgroup 占用约 202MiB，独立检查时约 198MiB，Linux 回归结束后约 133MiB，系统可用内存约 368MiB。不能将这些空闲观察值写成 45 分钟真实面试、模型报告生成或多场并发的峰值。

## 代理准备的实际问题

宝塔镜像通过 DNF exclude 过滤 Nginx，安装返回 `All matches were filtered out by exclude filtering`。未修改该规则，改为准备独立 Caddy 入口。

Caddy 2.11.7 官方 tar.gz 的 SHA-256：`727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b`；解包后的 Linux amd64 二进制 SHA-256：`678ade3bfc088749c81a681adc603333ee0bb023b6a6cfe3c0f58bef8ff854e9`。本机从官方 GitHub 下载的完整文件已验证。服务器直接下载超时；浏览器扩展缺少 file-URL 权限，程序化上传未成功，没有更改扩展权限。

最终在服务器从同一官方 GitHub 资产按 HTTP Range 分段下载（4 个并发、每段 1MiB），检查每段 Content-Range / 长度，合并后校验完整归档及二进制摘要，均一致。不是从第三方镜像获取文件，也没有关闭 TLS 校验。之后实际执行了完整 Caddy 配置 validate、临时本机代理的 smoke，并关闭临时代理；正式服务保持未启用。证书未申请，公网 HTTPS / WSS 仍未验证。

应用手动重启后的当次 cgroup 观察值约 187MiB，系统可用内存约 354MiB，swap 使用仍为 0；这是空业务状态下的观察。

## 尚未完成的上线项

- 正式 DNS / HTTPS / 证书申请与续期验收。
- 飞书正式登录、管理员 / HR 范围、应用入口和回调配置；服务器上目前没有可用的开发身份入口。
- 腾讯云 ASR 与真实大模型凭据及真实语音 / 追问 / 报告验证；未测试模型响应质量、费用和长面试峰值。
- 本地岗位、候选人、面试记录、录音和现有 Obsidian 内容的迁移选择、迁移与核对。
- 异地备份、磁盘加密验证、容量告警、备份保留期清理和真实业务恢复演练。
- 代码回滚已有操作说明；未验证真实生产数据的数据库回滚。

本次没有修改 DNS 或云防火墙，没有发布正式入口，没有迁移本地招聘数据，也没有把任何密钥或候选人材料写入 Git。
