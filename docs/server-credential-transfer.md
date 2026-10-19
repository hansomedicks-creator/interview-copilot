# 服务配置加密迁移

用途：本机已保存飞书、腾讯云 ASR、DeepSeek 服务配置，但服务器没有受控 SSH 文件通道时，通过已有腾讯云自动化助手传递**公钥和密文**，不把明文配置写入云端命令历史。不新增 SSH 权限，不导出简历、录音、面试记录或知识库。

## 安全边界

- 操作前需确认具体源、目标服务器和配置范围。当前工具限定目标为 `interview-copilot.ink@124.220.191.54`，模型 API 限定 `https://api.deepseek.com`。
- 服务端 root 生成 RSA 3072 位一次性接收密钥；私钥保存在 root-only `/run/interview-copilot-transfer/<随机值>`，只展示公有证书和 SHA-256 指纹。应通过已认证的服务器控制台核对指纹；自签名公有证书仅用于加密，不安装到浏览器信任库。
- 本机通过 OpenSSL CMS 的 AES-256-GCM 和 RSA-OAEP/SHA-256 加密。明文只进入内存与 OpenSSL 标准输入，不作为命令参数或导出文件。密文不包含发送方签名，来源可信性仍依赖已认证、受控的控制台上传流程；不能接受来源不明的密文。
- 仅允许脚本 `ALLOWED` 中的飞书身份/凭据、模型名称与凭据、ASR 配置。**不会覆盖**服务器数据库 URL、会话密钥、production 模式、正式回调 URL、服务器路径或数据保留设置。通知开关保持原值。
- 接收窗口 1 小时；指纹不符、认证标签篡改、未知字段、控制字符、缺失管理员配置、重复导入都会拒绝。导入不修改数据库、不开公网、不调用模型/ASR、不重启服务。
- 服务端原配置保留 root-only `production.env.before-transfer-<随机值>`。新文件以 0600 权限原子替换；消费后删除此次生成的接收私钥。删除是 unlink，不声称安全擦除。导入前失败的接收私钥仍留在 root-only `/run`，应由管理员处理过期目录；机器重启通常清空 `/run`。
- 已登录 root 或控制平面被攻破时，本方案不能保护服务器内的密钥；密文也不能替代 root-only 访问控制、供应商最小权限和密钥轮换。

## 执行顺序

1. 将审核后的脚本同步到服务器，在服务器执行：

   ```bash
   python3 /opt/interview-copilot/source/deploy/linux/credential_transfer.py init
   ```

   输出只含目标、接收目录、过期时间、指纹、公有证书。私钥不得复制到本机或控制台命令内容。

2. 本机保存公有证书，**从同一个已认证的服务器结果核对指纹**后执行。Windows 需要显式传入本机 OpenSSL 3 可执行文件：

   ```text
   python credential_transfer.py --openssl <openssl绝对路径> seal --env-file <本地.env.local> --certificate <recipient.pem> --fingerprint <控制台核对的64位SHA256> --output <services.cms>
   ```

   输出路径必须为不存在的新文件；只产生 CMS 密文。不要上传整个 `.env.local`，不要用明文或仅 Base64 编码的配置代替密文。

3. 上传密文到同一台服务器，核对传输前后的 SHA-256。密文可以经过自动化助手，但仍应限制记录访问。将 `init` 输出的接收目录原样传入：

   ```text
   python3 credential_transfer.py install --transfer-dir <init输出的目录> --envelope <services.cms>
   ```

4. 在无正在进行面试时重启应用，运行 `manage.py status` 与 `manage.py smoke`。`status` 仅证明配置字段存在，不证明真实 API 可用；另需对飞书 OAuth、模型、ASR 完成合成数据验收。飞书平台须单独添加正式回调地址，保留本地地址供回退。

参考：[OpenSSL CMS 官方参数](https://docs.openssl.org/3.5/man1/openssl-cms/)。

## 当前验证范围

已用合成数据验证：允许字段、无关设置保留、引号/中文/特殊字符、指纹拒绝、加密往返、认证标签篡改拒绝、一次性消费。真实生产配置迁移是否执行，应以服务器执行记录为准，不以工具文件存在为准。
