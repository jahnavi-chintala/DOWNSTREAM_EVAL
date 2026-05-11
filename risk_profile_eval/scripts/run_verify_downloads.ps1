<#
.SYNOPSIS
  Batch Risk Profile eval → %USERPROFILE%\Downloads\eval_docs\risk_profile

  Discovers every Risk Profile JSON under the input tree (one output folder per protocol id).

  Each study: eval_docs\risk_profile\{study_id}\  →  .json, .yaml, .docx only

  Inputs: %USERPROFILE%\Downloads\outputs_actual (recursive)

  Env: RISK_PROFILE_GENERATOR_DIR — input root (default: Downloads\outputs_actual)
       RISK_PROFILE_EVAL_DOCS_DIR — output parent (default: Downloads\eval_docs\risk_profile)
       RISK_PROFILE_EVAL_SKIP_CLEAN — set to 1 to skip deleting output folder before run

  Legacy (fixed verify list only): python run_batch_eva_docs.py ... --verify-set-only
#>
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Downloads = Join-Path $env:USERPROFILE "Downloads"
$DefaultInput = Join-Path $Downloads "outputs_actual"
# Default: ...\Downloads\eval_docs\risk_profile
$DefaultOut = Join-Path (Join-Path $Downloads "eval_docs") "risk_profile"

$InputDir = if ($env:RISK_PROFILE_GENERATOR_DIR) { $env:RISK_PROFILE_GENERATOR_DIR } else { $DefaultInput }
$OutputDir = if ($env:RISK_PROFILE_EVAL_DOCS_DIR) { $env:RISK_PROFILE_EVAL_DOCS_DIR } else { $DefaultOut }

if (-not (Test-Path -LiteralPath $InputDir)) {
    New-Item -ItemType Directory -Force -Path $InputDir | Out-Null
    Write-Host "Created inputs folder: $InputDir" -ForegroundColor Yellow
}

if ($env:RISK_PROFILE_EVAL_SKIP_CLEAN -ne "1") {
    if (Test-Path -LiteralPath $OutputDir) {
        Write-Host "Clearing: $OutputDir (per-study subfolders will be recreated)" -ForegroundColor DarkCyan
        Get-ChildItem -LiteralPath $OutputDir -Force | Remove-Item -Recurse -Force -ErrorAction Stop
    }
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Risks = Join-Path $Root "data\risk_profile_ground_truth.csv"
$Factors = Join-Path $Root "data\critical_factors_ground_truth.csv"
if (-not (Test-Path $Risks)) { Write-Error "Missing: $Risks"; exit 1 }
if (-not (Test-Path $Factors)) { Write-Error "Missing: $Factors"; exit 1 }

$nJson = @(Get-ChildItem -LiteralPath $InputDir -Recurse -Filter "*.json" -ErrorAction SilentlyContinue).Count
Write-Host "Inputs:  $InputDir  ($nJson JSON under tree)"
Write-Host "Outputs: $OutputDir\{study_id}\  (json, yaml, docx per study)"
Write-Host ""

$py = Join-Path $Root "run_batch_eva_docs.py"
& python $py --generator_output_dir $InputDir --ground_truth_risks $Risks --ground_truth_factors $Factors --output_dir $OutputDir

exit $LASTEXITCODE
