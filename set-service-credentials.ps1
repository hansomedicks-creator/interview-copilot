param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Feishu", "Tencent", "DeepSeek")]
    [string]$Section,
    [string]$FeishuAppId = ""
)

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

function Put-Default {
    param([string]$Name, [string]$Value)
    if (-not $script:values.Contains($Name)) { $script:values[$Name] = $Value }
}

function Read-SecretValue {
    param([string]$Label, [string]$Current)
    $status = if ($Current) { "已设置，直接回车保留" } else { "未设置" }
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

function Read-PlainValue {
    param([string]$Label, [string]$Current, [string]$Default = "")
    $shown = if ($Current) { $Current } elseif ($Default) { $Default } else { "未设置" }
    $inputValue = Read-Host "$Label [当前: $shown，直接回车保留]"
    if ($inputValue) { return $inputValue.Trim() }
    if ($Current) { return $Current }
    return $Default
}

Put-Default "INTERVIEW_ENV" "development"
Put-Default "INTERVIEW_DATABASE_URL" "sqlite:///./interview-copilot.db"
Put-Default "INTERVIEW_PUBLIC_BASE_URL" "http://127.0.0.1:8000"
Put-Default "INTERVIEW_RETENTION_DAYS" "90"
Put-Default "INTERVIEW_MAX_RETENTION_DAYS" "180"
Put-Default "INTERVIEW_REQUIRE_RECORDING_NOTICE" "true"
Put-Default "INTERVIEW_RECORDING_DIR" "./data/recordings"
Put-Default "INTERVIEW_MAX_AUDIO_CHUNK_BYTES" "65536"
Put-Default "INTERVIEW_AUDIO_SAMPLE_RATE" "16000"
Put-Default "INTERVIEW_AUDIO_CHANNELS" "1"
Put-Default "INTERVIEW_PIPECAT_ENABLED" "true"
Put-Default "INTERVIEW_PROVIDER_MODE" "mock"
Put-Default "INTERVIEW_ASR_PROVIDER" "disabled"
Put-Default "INTERVIEW_ASR_ENGINE_MODEL_TYPE" "16k_zh_en_speaker_2.0"
Put-Default "INTERVIEW_SESSION_HOURS" "12"
Put-Default "INTERVIEW_KNOWLEDGE_VAULT_DIR" "D:\Interview-Knowledge"
Put-Default "INTERVIEW_KNOWLEDGE_VAULT_NAME" "Interview-Knowledge"

if (-not $values["INTERVIEW_SESSION_SECRET"] -or $values["INTERVIEW_SESSION_SECRET"] -eq "development-only-change-me") {
    $randomBytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($randomBytes)
    $values["INTERVIEW_SESSION_SECRET"] = [Convert]::ToBase64String($randomBytes)
}

Clear-Host
Write-Host "Interview Copilot - $Section 凭证配置" -ForegroundColor Cyan
Write-Host "密钥不会回显，只保存在本机 .env.local，且已被 Git 忽略。`n"

switch ($Section) {
    "Feishu" {
        $defaultAppId = if ($FeishuAppId) { $FeishuAppId } else { [string]$values["FEISHU_APP_ID"] }
        $values["FEISHU_APP_ID"] = Read-PlainValue "飞书 App ID" $values["FEISHU_APP_ID"] $defaultAppId
        $values["FEISHU_APP_SECRET"] = Read-SecretValue "飞书 App Secret" $values["FEISHU_APP_SECRET"]
        $values["FEISHU_REDIRECT_URI"] = "http://127.0.0.1:8000/api/v1/auth/feishu/callback"
        $values["FEISHU_OAUTH_SCOPES"] = "auth:user.id:read"
        Put-Default "FEISHU_NOTIFICATIONS_ENABLED" "false"
        Put-Default "FEISHU_HR_OPEN_IDS" ""
        Put-Default "FEISHU_ADMIN_OPEN_IDS" ""
    }
    "Tencent" {
        $values["TENCENT_ASR_APP_ID"] = Read-PlainValue "腾讯云 AppID（纯数字）" $values["TENCENT_ASR_APP_ID"]
        $values["TENCENT_ASR_SECRET_ID"] = Read-SecretValue "腾讯云 SecretId" $values["TENCENT_ASR_SECRET_ID"]
        $values["TENCENT_ASR_SECRET_KEY"] = Read-SecretValue "腾讯云 SecretKey" $values["TENCENT_ASR_SECRET_KEY"]
        $values["TENCENT_ASR_HOTWORDS"] = Read-PlainValue "热词表 ID（可留空）" $values["TENCENT_ASR_HOTWORDS"]
        $values["INTERVIEW_ASR_PROVIDER"] = "tencent"
    }
    "DeepSeek" {
        $values["INTERVIEW_LLM_API_KEY"] = Read-SecretValue "DeepSeek API Key" $values["INTERVIEW_LLM_API_KEY"]
        $values["INTERVIEW_LLM_BASE_URL"] = "https://api.deepseek.com"
        $values["INTERVIEW_LLM_MODEL"] = "deepseek-v4-flash"
        $values["INTERVIEW_LLM_TIMEOUT_SECONDS"] = "20"
        $values["INTERVIEW_LLM_MAX_CONTEXT_CHARS"] = "12000"
        $values["INTERVIEW_LLM_ALLOW_INSECURE_HTTP"] = "false"
        $values["INTERVIEW_PROVIDER_MODE"] = "production"
    }
}

$lines = @(
    "# Interview Copilot 本机私密配置；请勿发送给他人或提交到 Git。",
    "# 由 set-service-credentials.ps1 生成。"
)
foreach ($item in $values.GetEnumerator()) {
    $value = if ($null -eq $item.Value) { "" } else { [string]$item.Value }
    if ($value.Contains("`r") -or $value.Contains("`n")) { throw "$($item.Key) 不能包含换行符" }
    $lines += "$($item.Key)=$value"
}
$utf8NoBom = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllLines($configFile, $lines, $utf8NoBom)

Write-Host "`n$Section 凭证已保存。窗口可安全关闭。" -ForegroundColor Green
Read-Host "按回车键结束"
