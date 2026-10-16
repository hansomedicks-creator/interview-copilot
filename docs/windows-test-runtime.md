# Windows 测试运行环境故障排查

## 2026-10-16 实测

- 当前 Codex 使用 Windows MXC 沙箱。
- 沙箱内仅运行 `asyncio.run(asyncio.sleep(0))`，8 秒仍未返回。限时线程栈停在 `socket._fallback_socketpair -> accept`，上层为 `asyncio` 的 `_make_self_pipe`。
- 该最小复现不导入项目，也不调用模型、飞书或腾讯云。因此它能证明异步运行环境被阻塞，但不能单凭该堆栈判断具体 Windows 规则。
- 同一 Python、同一代码通过明确授权的沙箱外命令可运行完整隔离测试。
- 新测试入口第一次回归暴露出较长临时目录导致 Obsidian 合成测试写文件失败；缩短随机目录后，定向测试恢复通过。入口保留短目录名，避免叠加 Windows 路径长度限制。
- 当前聊天可写工作区是 C 盘路径；日常运行项目在 D 盘。D 盘写入需单独授权，不能靠项目代码扩展工作区权限。

## 固定测试入口

在项目根目录使用项目虚拟环境：

```powershell
.\backend\.venv\Scripts\python.exe .\backend\scripts\run_tests.py -q
```

仅检查 Python 异步运行环境：

```powershell
.\backend\.venv\Scripts\python.exe .\backend\scripts\run_tests.py --check-only
```

入口先以 8 秒超时检测事件循环。失败返回退出码 78，明确表示测试尚未运行，不再无限卡在 TestClient 启动。检测成功后，将 pytest 临时文件放在项目内的独立 `.pytest-run-*` 目录，清除继承的 `INTERVIEW_*` 配置，并使用 mock 模型和隔离测试数据。

## 正确处置

1. 若出现 `ASYNC_RUNTIME_BLOCKED`，使用已明确授权的测试命令，或在本人正常 PowerShell 项目终端执行同一入口。不要把失败或超时记为测试通过。
2. 不要为了运行测试改成全盘可写、关闭防火墙、关闭沙箱或使用不受限执行模式。
3. 当前受管沙箱的全局网络策略无法由项目修复。若希望测试原生运行在沙箱内，需要通过 Codex 的权限界面或管理员核查本机回环策略；项目不自行修改配置。
4. 更换工作目录时，应让聊天绑定真实项目目录。路径迁移问题和 Python 回环阻塞是两类问题，应分别验证。

官方参考：[Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)。官方说明 MXC 兼容性需检查有效网络策略和本地绑定权限。该项目仅提供有界检测和经过授权的测试路径，未宣称修复 Windows / Codex 本身的 MXC 实现。
