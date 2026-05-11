@echo off
REM Batch → %USERPROFILE%\Downloads\eval_docs\risk_profile\{study_id}\  (json, yaml, docx)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_verify_downloads.ps1"
exit /b %ERRORLEVEL%
