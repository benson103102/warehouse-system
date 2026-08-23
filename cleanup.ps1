# ============================================================
#  warehouse-system 資料夾清理腳本
#  用途：刪除「非程式碼、可重建、或重複」的檔案，讓資料夾變乾淨，
#        推上 GitHub / 雲端時不會帶一堆不必要的東西給別人看。
#  用法：把這個檔案放在 warehouse-system 這個資料夾「最上層」，
#        滑鼠右鍵點它 →「使用 PowerShell 執行」（或見下方指令）。
#  安全性：只會刪除下面明確列出的幾類東西，不會動到
#          frontend / backend/app（原始碼）／README／Dockerfile。
# ============================================================

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Write-Host "專案路徑：$root"
Write-Host ""

# 1) Python 虛擬環境 backend/.venv
#    這是「backend\requirements.txt 裝好的套件」存放處，本來就是
#    跑 pip install 時自動產生的，砍掉不影響程式碼，之後要重建只要：
#    cd backend
#    python -m venv .venv
#    .venv\Scripts\pip install -r requirements.txt
$venv = Join-Path $root "backend\.venv"
if (Test-Path $venv) {
    Write-Host "[1/4] 刪除 backend\.venv（Python 虛擬環境，可重建）..."
    Remove-Item -Recurse -Force $venv
} else {
    Write-Host "[1/4] 找不到 backend\.venv，略過。"
}

# 2) 所有 __pycache__ 資料夾（Python 執行時自動產生的編譯快取，可重建）
Write-Host "[2/4] 刪除所有 __pycache__ 資料夾（Python 執行快取，可重建）..."
Get-ChildItem -Path $root -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Write-Host "      刪除 $($_.FullName)"
        Remove-Item -Recurse -Force $_.FullName
    }

# 3) 根目錄的教學／開發紀錄 md 檔（部署到 GitHub/雲端後用不到，
#    且內含你的 GitHub 帳號、GCP 專案代號等個人設定細節）
$mdFiles = @(
    "大檔案上傳修正教學.md",
    "推送到GitHub教學.md",
    "Google_Cloud_Run_部署教學.md",
    "移植記錄_2026-08.md"
)
Write-Host "[3/4] 刪除教學／開發紀錄 md 檔..."
foreach ($f in $mdFiles) {
    $p = Join-Path $root $f
    if (Test-Path $p) {
        Write-Host "      刪除 $f"
        Remove-Item -Force $p
    }
}

# 4) backend/data/uploads/u1 底下重複的上傳檔，只留最新一份
#    （同一份 90MB 的檔案重複上傳測試了好幾次，其餘的可以刪掉省空間）
$uploadDir = Join-Path $root "backend\data\uploads\u1"
if (Test-Path $uploadDir) {
    Write-Host "[4/4] 清理重複的上傳檔（只保留最新一份）..."
    $files = Get-ChildItem -Path $uploadDir -File | Sort-Object LastWriteTime -Descending
    if ($files.Count -gt 1) {
        $keep = $files[0]
        Write-Host "      保留：$($keep.Name)"
        $files | Select-Object -Skip 1 | ForEach-Object {
            Write-Host "      刪除：$($_.Name)"
            Remove-Item -Force $_.FullName
        }
    } else {
        Write-Host "      沒有重複檔案，略過。"
    }
} else {
    Write-Host "[4/4] 找不到 backend\data\uploads\u1，略過。"
}

Write-Host ""
Write-Host "清理完成！"
Read-Host "按 Enter 鍵關閉這個視窗"
