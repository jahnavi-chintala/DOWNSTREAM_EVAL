<#
.SYNOPSIS
  Single-study Risk Profile eval → %USERPROFILE%\Downloads\eval_docs\risk_profile\{StudyId}

  Outputs **only**: .json, .yaml, .docx in that study folder.

  Example:
    .\run_eval_downloads.ps1 -StudyId C5091017

  For every protocol under outputs_actual, use run_verify_downloads.ps1 instead.
#>
param(
    [Parameter(Mandatory = $true)]
    [string] $StudyId,
    [string] $GeneratorJson = ""
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Downloads = Join-Path $env:USERPROFILE "Downloads"
$BaseRiskProfile = Join-Path (Join-Path $Downloads "eval_docs") "risk_profile"
$DefaultOut = Join-Path $BaseRiskProfile $StudyId
$Out = if ($env:RISK_PROFILE_EVAL_DOCS_DIR) { $env:RISK_PROFILE_EVAL_DOCS_DIR } else { $DefaultOut }

if ($env:RISK_PROFILE_EVAL_SKIP_CLEAN -ne "1") {
    if (Test-Path -LiteralPath $Out) {
        Write-Host "Clearing study folder: $Out" -ForegroundColor DarkCyan
        Remove-Item -LiteralPath $Out -Recurse -Force -ErrorAction Stop
    }
}
New-Item -ItemType Directory -Force -Path $Out | Out-Null

$Risks = Join-Path $Root "data\risk_profile_ground_truth.csv"
$Factors = Join-Path $Root "data\critical_factors_ground_truth.csv"

function Find-RiskProfileJson {
    param([string]$StudyId, [string[]]$SearchRoots)
    $sid = $StudyId
    $all = @()
    foreach ($dir in $SearchRoots) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        $all += Get-ChildItem -LiteralPath $dir -Recurse -Filter "*.json" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*$sid*" }
    }
    if (-not $all) { return $null }
    $ranked = $all | Sort-Object {
        $n = $_.Name.ToLowerInvariant()
        if ($n -match 'riskprofile|risk_profile|risk-profile') { 0 }
        elseif ($n -match 'usdm' -and $n -notmatch 'risk') { 2 }
        else { 1 }
    }, FullName
    return $ranked | Select-Object -First 1
}

if (-not $GeneratorJson) {
    $roots = @(
        (Join-Path $Downloads "outputs_actual"),
        (Join-Path $Root "data")
    )
    if ($env:RISK_PROFILE_GENERATOR_DIR) {
        $roots = @($env:RISK_PROFILE_GENERATOR_DIR) + $roots
    }
    $hit = Find-RiskProfileJson -StudyId $StudyId -SearchRoots $roots
    if ($hit) { $GeneratorJson = $hit.FullName }
}

if (-not $GeneratorJson -or -not (Test-Path -LiteralPath $GeneratorJson)) {
    Write-Error "No Risk Profile JSON for $StudyId. Use outputs_actual or pass -GeneratorJson."
    exit 1
}

Write-Host "Generator: $GeneratorJson"
Write-Host "Output:    $Out"
Write-Host ""

$py = Join-Path $Root "run_eval.py"
& python $py --generator_json $GeneratorJson --ground_truth_risks $Risks --ground_truth_factors $Factors --study_id $StudyId --output_dir $Out --no-supplementary --force-scenario1-when-no-gt

exit $LASTEXITCODE
