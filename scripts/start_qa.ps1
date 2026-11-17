param(
    [string]$IdsFile = (Join-Path $PSScriptRoot '..' 'demo-ids.local.json'),
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$ids = Get-Content -LiteralPath $IdsFile -Raw | ConvertFrom-Json
if (-not $ids.foundryProjectEndpoint -or -not $ids.foundryAgentName) {
    throw 'Run scripts\configure_foundry_iq.py before starting Q&A.'
}
$env:QA_BACKEND = 'foundry'
$env:FOUNDRY_PROJECT_ENDPOINT = $ids.foundryProjectEndpoint
$env:FOUNDRY_AGENT_NAME = $ids.foundryAgentName
$env:FOUNDRY_AUTH_MODE = 'azure-cli'
$env:FOUNDRY_TENANT_ID = $ids.tenantId
$env:FOUNDRY_SUBSCRIPTION_ID = $ids.subscriptionId
Push-Location (Join-Path $PSScriptRoot '..')
try {
    & $Python -m frontend.app
    if ($LASTEXITCODE -ne 0) { throw 'Q&A server exited unsuccessfully.' }
} finally {
    Pop-Location
}
