# 야간 배치: data/raw 의 인허가 CSV(증분 파일 포함) → data/processed 재계산
# 작업 스케줄러 등록 예 (매일 03:00):
#   schtasks /Create /SC DAILY /ST 03:00 /TN "DaeguLifecycleBatch" /TR "powershell -ExecutionPolicy Bypass -File C:\claude_workspace\daegu_lifecycle_agent\run_batch.ps1"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force logs | Out-Null
python -m pipeline.build *>> "logs\batch_$(Get-Date -Format yyyyMMdd).log"
