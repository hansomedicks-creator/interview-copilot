$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configFile = Join-Path $projectDir ".env.local"
$values = [ordered]@{}

if (Test-Path -LiteralPath $configFile) {
    foreach ($line in Get-Content -LiteralPath $configFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) { continue }
        $values[$trimmed.Substring(0, $separator).Trim()] = $trimmed.Substring($separator + 1)
    }
}

function Read-PlainValue {
    param([string]$Label, [string]$Current, [string]$Default = "")
    $shown = if ($Current) { $Current } elseif ($Default) { $Default } else { "未设置" }
    $inputValue = Read-Host "$Label [当前: $shown，直接回车保留]"
    if ($inputValue) { return $inputValue.Trim() }
    if ($Current) { return $Current }
    return $Default
}

function Read-SecretValue {
    param([string]$Label, [string]$Current)
    $status = if ($Current) { "已设置，直接回车保留" } else { "未设置，直接回车跳过" }
    $secure = Read-Host "$Label [$status]" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    if ($plain) { return $plain }
    return $Current
}

function Put-Value {
    param([string]$Name, [AllowEmptyString()][string]$Value)
    $script:values[$Name] = $Value
}

Clear-Host
Write-Host "Interview Copilot 正式服务配置" -ForegroundColor Cyan
Write-Host "凭证仅写入本机 .env.local，不会显示在屏幕上，也不会提交到 Git。"
Write-Host "可先跳过尚未申请的项目，之后重新运行本脚本补充。`n"

Put-Value "INTERVIEW_ENV" "development"
Put-Value "INTERVIEW_DATABASE_URL" (Read-PlainValue "数据库地址" $values["INTERVIEW_DATABASE_URL"] "sqlite:///./interview-copilot.db")
Put-Value "INTERVIEW_PUBLIC_BASE_URL" (Read-PlainValue "本机访问地址" $values["INTERVIEW_PUBLIC_BASE_URL"] "http://127.0.0.1:8000")
Put-Value "INTERVIEW_RETENTION_DAYS" (Read-PlainValue "录音与逐字稿保留天数" $values["INTERVIEW_RETENTION_DAYS"] "90")
Put-Value "INTERVIEW_MAX_RETENTION_DAYS" "180"
Put-Value "INTERVIEW_REQUIRE_RECORDING_NOTICE" "true"
Put-Value "INTERVIEW_RECORDING_DIR" (Read-PlainValue "录音存储目录" $values["INTERVIEW_RECORDING_DIR"] "./data/recordings")
Put-Value "INTERVIEW_MAX_AUDIO_CHUNK_BYTES" "65536"
Put-Value "INTERVIEW_AUDIO_SAMPLE_RATE" "16000"
Put-Value "INTERVIEW_AUDIO_CHANNELS" "1"
Put-Value "INTERVIEW_PIPECAT_ENABLED" "true"

Write-Host "`n[1/3] 飞书正式登录" -ForegroundColor Yellow
Put-Value "FEISHU_APP_ID" (Read-PlainValue "飞书 App ID" $values["FEISHU_APP_ID"])
Put-Value "FEISHU_APP_SECRET" (Read-SecretValue "飞书 App Secret" $values["FEISHU_APP_SECRET"])
Put-Value "FEISHU_REDIRECT_URI" (Read-PlainValue "飞书 OAuth 回调地址" $values["FEISHU_REDIRECT_URI"] "http://127.0.0.1:8000/api/v1/auth/feishu/callback")
Put-Value "FEISHU_OAUTH_SCOPES" "auth:user.id:read"
Put-Value "FEISHU_NOTIFICATIONS_ENABLED" "false"
Put-Value "FEISHU_HR_OPEN_IDS" $values["FEISHU_HR_OPEN_IDS"]
Put-Value "FEISHU_ADMIN_OPEN_IDS" $values["FEISHU_ADMIN_OPEN_IDS"]

Write-Host "`n[2/3] 腾讯云实时语音识别" -ForegroundColor Yellow
Put-Value "INTERVIEW_ASR_PROVIDER" "tencent"
Put-Value "INTERVIEW_ASR_ENGINE_MODEL_TYPE" "16k_zh_en_speaker_2.0"
Put-Value "TENCENT_ASR_APP_ID" (Read-PlainValue "腾讯云 AppID（纯数字）" $values["TENCENT_ASR_APP_ID"])
Put-Value "TENCENT_ASR_SECRET_ID" (Read-SecretValue "腾讯云 SecretId" $values["TENCENT_ASR_SECRET_ID"])
Put-Value "TENCENT_ASR_SECRET_KEY" (Read-SecretValue "腾讯云 SecretKey" $values["TENCENT_ASR_SECRET_KEY"])
Put-Value "TENCENT_ASR_HOTWORDS" (Read-PlainValue "热词表 ID（可留空）" $values["TENCENT_ASR_HOTWORDS"])

Write-Host "`n[3/3] DeepSeek 真实大模型" -ForegroundColor Yellow
Put-Value "INTERVIEW_PROVIDER_MODE" "production"
Put-Value "INTERVIEW_LLM_BASE_URL" (Read-PlainValue "模型接口地址" $values["INTERVIEW_LLM_BASE_URL"] "https://api.deepseek.com")
Put-Value "INTERVIEW_LLM_MODEL" (Read-PlainValue "模型名称" $values["INTERVIEW_LLM_MODEL"] "deepseek-v4-flash")
Put-Value "INTERVIEW_LLM_PLANNING_MODEL" (Read-PlainValue "面试前深度问题模型" $values["INTERVIEW_LLM_PLANNING_MODEL"] "deepseek-v4-pro")
Put-Value "INTERVIEW_LLM_API_KEY" (Read-SecretValue "DeepSeek API Key" $values["INTERVIEW_LLM_API_KEY"])
Put-Value "INTERVIEW_LLM_TIMEOUT_SECONDS" "20"
Put-Value "INTERVIEW_LLM_MAX_CONTEXT_CHARS" "12000"
Put-Value "INTERVIEW_LLM_ALLOW_INSECURE_HTTP" "false"

if (-not $values["INTERVIEW_SESSION_SECRET"] -or $values["INTERVIEW_SESSION_SECRET"] -eq "development-only-change-me") {
    $randomBytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($randomBytes)
    Put-Value "INTERVIEW_SESSION_SECRET" ([Convert]::ToBase64String($randomBytes))
}
Put-Value "INTERVIEW_SESSION_HOURS" "12"
Put-Value "INTERVIEW_KNOWLEDGE_VAULT_DIR" (Read-PlainValue "Obsidian 知识库目录" $values["INTERVIEW_KNOWLEDGE_VAULT_DIR"] "D:\Interview-Knowledge")
Put-Value "INTERVIEW_KNOWLEDGE_VAULT_NAME" (Read-PlainValue "知识库名称" $values["INTERVIEW_KNOWLEDGE_VAULT_NAME"] "Interview-Knowledge")

$lines = @(
    "# Interview Copilot 本机私密配置；请勿发送给他人或提交到 Git。",
    "# 由 configure-interview-copilot.ps1 生成。"
)
foreach ($item in $values.GetEnumerator()) {
    $value = if ($null -eq $item.Value) { "" } else { [string]$item.Value }
    if ($value.Contains("`r") -or $value.Contains("`n")) {
        throw "$($item.Key) 不能包含换行符"
    }
    $lines += "$($item.Key)=$value"
}
$utf8NoBom = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllLines($configFile, $lines, $utf8NoBom)

Write-Host "`n配置已安全保存到本机。请关闭旧服务，再双击 start-interview-copilot.cmd 重新启动。" -ForegroundColor Green
Read-Host "按回车键结束"
