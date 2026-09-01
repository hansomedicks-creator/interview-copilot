# Interview Copilot 架构说明

本文描述当前仓库中的真实实现，不将接口预留或未来设想写成已经上线的能力。

## 总体结构

```text
Browser SPA
  ├─ REST API ─────────────────────────────┐
  ├─ live WebSocket（字幕/状态）           │
  └─ audio WebSocket（16 kHz PCM）         │
                                           ▼
                                     FastAPI application
                    ┌──────────────────────┼──────────────────────┐
                    ▼                      ▼                      ▼
             domain services       provider adapters       SQLAlchemy
                    │               ├─ Tencent ASR               │
                    │               ├─ OpenAI-compatible LLM     │
                    │               └─ Feishu notifications      │
                    ▼                                             ▼
             Obsidian filesystem                         SQLite / PostgreSQL
                                                                  │
                                                                  ▼
                                                           local WAV files
```

## 代码布局

```text
backend/
  app/
    main.py                 FastAPI 应用、路由、WebSocket 和编排
    models.py               SQLAlchemy 数据模型
    schemas.py              API 输入输出模型
    config.py               环境变量配置及启动校验
    database.py             数据库引擎、会话及当前轻量迁移
    services/               面试计划、证据、评分、报告、治理等领域逻辑
    providers/              Mock、OpenAI 兼容模型、飞书通知
    asr/                    ASR Provider 抽象和腾讯云实时 ASR
    audio/                  音频帧桥和 WAV 录音
    static/                 原生 HTML/CSS/JavaScript 前端
  tests/                    pytest API、ASR、证据和岗位语义测试
contracts/                  与筛选项目交换数据的 JSON Schema
docs/                       产品、架构、决策、问题和运维文档
```

当前没有独立 `frontend/` 工程。前端由 FastAPI 直接提供 `static/index.html`、`app.js` 和 `styles.css`。

## 后端

### Web 层

- FastAPI 提供 REST API、HTML 首页和两个 WebSocket 端点；
- HTTP 中间件校验签名会话并按 HR、管理员、面试官控制访问；
- 面试官访问面试资源时还会校验 `InterviewAssignment`；
- 飞书 OAuth 回调在服务端交换用户信息并写入 HttpOnly 会话 Cookie。

### 领域服务

`services/` 中承载以下逻辑：

- 岗位和公司人才画像版本；
- 面试计划、JD/简历结合问题和跨轮信息；
- 问答覆盖、证据提取、证据聚合与人工确认；
- 自由对话分析、回答逻辑核验和评分；
- 面试官质量、行动中心和通知队列；
- 最终审核、版本化报告和数据保留；
- 历史招聘样本脱敏导入；
- 知识提案、Obsidian 发布和系统文档同步。

### 数据层

- ORM：SQLAlchemy 2；
- 本地默认：SQLite；
- 可配置：PostgreSQL（通过 `INTERVIEW_DATABASE_URL`）；
- 当前没有 Alembic，少量新增字段由 `Database.create_all()` 中的启动检查和 `ALTER TABLE` 处理；
- 主要实体包括候选人、岗位、申请、面试轮次、任务分配、逐字稿、录音、证据、评分、报告、通知、审计、知识提案和人才画像版本。

## 实时音频与字幕

当前链路：

```text
navigator.mediaDevices.getUserMedia
  → Web Audio ScriptProcessor
  → 浏览器降采样为 mono 16 kHz PCM s16le
  → /ws/interviews/{id}/audio
  ├─ WAV recording session
  ├─ Pipecat InputAudioRawFrame bridge
  └─ ASR Provider
       → Tencent realtime ASR WebSocket
       → final/interim transcript events
       → speaker mapping and correction
       → TranscriptSegment persistence
       → live-state broadcast
```

两个边界需要明确：

1. Pipecat 当前只负责把 PCM 包装为 `InputAudioRawFrame`，尚未构成完整的 Pipecat transport/pipeline；
2. 前端仍使用 `ScriptProcessor` 和应用层 WebSocket，代码已明确将 AudioWorklet/WebRTC 作为生产演进方向。

ASR 短时断线时后端会尝试重建一次会话；无论 ASR 是否可用，WAV 录音链路都保持独立。腾讯云分配的声源编号会结合问答语义映射为面试官或候选人，低置信度结果允许人工纠正。

## AI 链路

```text
岗位、JD、画像、轮次职责
+ 脱敏简历上下文
+ 当前问题和候选人连续回答
+ 前轮已确认事实 / 待验证事项
                    ↓
         Intelligence Provider
       ├─ Mock local provider
       └─ OpenAI-compatible provider
                    ↓
             structured JSON
                    ↓
   本地 schema、原文引用、片段归属和能力范围校验
                    ↓
追问建议 / 证据候选 / 评分草稿 / 下一轮待验证问题
                    ↓
              人工确认与提交
```

OpenAI 兼容 Provider 支持实时分析模型和面试前规划模型分别配置。模型输出不能直接入库为最终招聘结论：证据引文必须在允许的候选人片段中逐字存在，能力项和问题也必须属于当前面试上下文。

当模型不可用时，系统回退到 Mock/本地规则以维持页面和基础证据流程，但回退结果不应被描述为与语义模型等价。

## 外部集成

| 集成 | 当前实现 |
|---|---|
| 飞书 OAuth | 服务端授权码交换、用户身份映射、签名 Cookie |
| 飞书通知 | 服务器获取应用令牌，HR 人工选择后发送消息 |
| 飞书视频会议 | 未实现自动入会、独立音轨或会中机器人 |
| 腾讯云 ASR | 实时 WebSocket、音频节奏控制、话者 ID 和重连 |
| 大模型 | OpenAI-compatible Chat Completions，支持 Mock/Production |
| Obsidian | 本地 Vault 文件写入、审批边界、系统文档确认同步 |
| 北森 | CSV/XLSX 历史样本预览和脱敏写入；没有在线 API |
| boss-recruiting-agent | 仅有 `contracts/recruiting-context.schema.json` 契约 |

## 安全与数据边界

- 所有正式 Key 仅通过服务器环境变量提供；
- `.env.local`、数据库、录音、上传文件和招聘导出均被 Git 忽略；
- 录音与 AI 分析存在开始前告知门禁；
- 下载录音、逐字稿和报告前校验角色与面试分配；
- 关键下载、清理、报告锁定和阶段决定写入审计；
- Obsidian 只接收经过审批的脱敏结构化知识；
- 数据清理需要 HR 二次确认，当前不会后台静默删除。

## 部署形态

### 本地开发

- SQLite；
- 本地 WAV；
- Mock 或通过 `.env.local` 配置真实 Provider；
- `uvicorn --reload`。

### Docker 骨架

`docker-compose.yml` 提供 FastAPI + PostgreSQL 的基础组合，但仍使用示例密码、Mock Provider 和本地 volume。它是部署骨架，不是可直接承载真实招聘数据的生产方案。

### 正式生产仍需补齐

- HTTPS 与正式域名；
- 受控的 PostgreSQL 实例和数据库迁移机制；
- 加密对象存储或受控持久卷；
- 密钥管理、备份、日志、监控、告警和恢复演练；
- WebRTC/媒体层的稳定性方案；
- ASR/LLM 的真实负载与质量评测；
- 候选人告知、权限和数据保留制度落地。

## 当前主要技术债

1. `main.py`、`app.js` 和 `styles.css` 体量较大，但没有业务收益前不做大规模框架重写；
2. 数据库缺少正式迁移工具；
3. WebSocket + ScriptProcessor 适合当前本地/受控试点，不是最终媒体架构；
4. 录音默认落本地文件系统；
5. 外部 Provider 缺少生产级监控、限流、熔断和多实例一致性设计；
6. 北森与飞书视频会议仍是集成边界或离线流程。

