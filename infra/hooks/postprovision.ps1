#!/usr/bin/env pwsh
# azd postprovision hook -- runs after infra/azd.bicep is deployed.
#
#  1. Writes demo-ids.local.json from the Bicep outputs (azd exports them as
#     environment variables), so every script in scripts/ works unchanged.
#  2. Stores the AI Search admin key in Key Vault (a warning, not a failure, when
#     policy blocks Key Vault public access -- the scripts fall back to az).
#  3. Optional: when DEMO_CORPUS_DIR is set, routes, uploads and builds the hybrid
#     index in the same run, so `azd up` ends with a working knowledge base.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..' '..')

function Get-Out([string]$Name) { [Environment]::GetEnvironmentVariable($Name) }

$summary = [ordered]@{
    resourceGroup                = Get-Out 'AZURE_RESOURCE_GROUP'
    region                       = Get-Out 'AZURE_LOCATION'
    environment                  = Get-Out 'DEMO_ENVIRONMENT'
    storageAccount               = Get-Out 'STORAGE_ACCOUNT'
    blobEndpoint                 = Get-Out 'BLOB_ENDPOINT'
    rawContainer                 = Get-Out 'RAW_CONTAINER'
    foundryResource              = Get-Out 'FOUNDRY_RESOURCE'
    foundryOpenAIEndpoint        = Get-Out 'FOUNDRY_OPENAI_ENDPOINT'
    documentIntelligenceEndpoint = Get-Out 'DOCUMENT_INTELLIGENCE_ENDPOINT'
    aiServicesSubdomainUrl       = Get-Out 'AI_SERVICES_SUBDOMAIN_URL'
    foundryProject               = Get-Out 'FOUNDRY_PROJECT'
    embeddingDeployment          = Get-Out 'EMBEDDING_DEPLOYMENT'
    embeddingModel               = Get-Out 'EMBEDDING_MODEL'
    chatDeployment               = Get-Out 'CHAT_DEPLOYMENT'
    chatModel                    = Get-Out 'CHAT_MODEL'
    visionDeployment             = Get-Out 'VISION_DEPLOYMENT'
    visionModel                  = Get-Out 'VISION_MODEL'
    frontierDeployment           = Get-Out 'FRONTIER_DEPLOYMENT'
    frontierModel                = Get-Out 'FRONTIER_MODEL'
    searchService                = Get-Out 'SEARCH_SERVICE'
    searchEndpoint               = Get-Out 'SEARCH_ENDPOINT'
    searchPrincipalId            = Get-Out 'SEARCH_PRINCIPAL_ID'
    keyVault                     = Get-Out 'KEY_VAULT'
    keyVaultUri                  = Get-Out 'KEY_VAULT_URI'
    fallbackServerDeployed       = Get-Out 'FALLBACK_SERVER_DEPLOYED'
    containerAppFqdn             = Get-Out 'CONTAINER_APP_FQDN'
    containerRegistry            = Get-Out 'CONTAINER_REGISTRY'
}
if (-not $summary.searchService -or -not $summary.keyVault) {
    Write-Host "[postprovision] Bicep outputs are missing from the azd environment (SEARCH_SERVICE / KEY_VAULT). Nothing written." -ForegroundColor Red
    exit 1
}

$tenant = if (Get-Out 'AZURE_TENANT_ID') { Get-Out 'AZURE_TENANT_ID' } else { az account show --query tenantId -o tsv }
$idsPath = Write-DemoIds -Summary $summary -TenantId $tenant -SubscriptionId (Get-Out 'AZURE_SUBSCRIPTION_ID') -RepoRoot $repoRoot
if ((Set-SearchAdminKeySecret -ResourceGroup $summary.resourceGroup -SearchService $summary.searchService -KeyVault $summary.keyVault) -eq 'no-key') {
    Write-Host "[postprovision] Could not read the Search admin key -- check the az login matches the azd environment." -ForegroundColor Red
    exit 1
}

# --- optional: ingest a corpus in the same run -----------------------------
$uploader = $null
if ("$(Get-Out 'DEMO_DEPLOY_UPLOADER')" -eq 'true') {
    $subnetId = Get-Out 'UPLOADER_SUBNET_ID'
    if (-not $subnetId) { throw 'DEMO_DEPLOY_UPLOADER requires UPLOADER_SUBNET_ID (a dedicated delegated subnet).' }
    $uploaderArgs = @{ IdsFile = $idsPath; SubnetId = $subnetId }
    $accessFile = Get-Out 'UPLOADER_ACCESS_FILE'
    if ($accessFile) { $uploaderArgs.AccessFile = $accessFile }
    $uploader = & (Join-Path $repoRoot 'scripts' 'deploy_uploader.ps1') @uploaderArgs
    if (-not $uploader.url) { throw 'Uploader deployment did not return a URL.' }
}
$corpus = Get-Out 'DEMO_CORPUS_DIR'
if ($corpus) {
    if (-not (Test-Path $corpus)) {
        throw "DEMO_CORPUS_DIR '$corpus' does not exist. No ready-to-query deployment can be reported."
    } else {
        $python = if (Get-Out 'DEMO_PYTHON') { Get-Out 'DEMO_PYTHON' } else { 'python' }
        $splitFlag = if ("$(Get-Out 'DEMO_SPLIT')" -eq 'false') { '--no-split' } else { '--split' }
        $ingest = Join-Path $repoRoot 'scripts' 'hybrid_ingest.py'
        Write-Host "[postprovision] Ingesting $corpus ($splitFlag) ..." -ForegroundColor Cyan
        if ($uploader) {
            & (Join-Path $repoRoot 'scripts' 'upload_pdfs.ps1') -AccessFile $uploader.accessFile `
                -SourceDir $corpus -AllowLarge:($splitFlag -eq '--no-split')
        } else {
            & $python $ingest --ids-file $idsPath $splitFlag --upload --source-dir $corpus
            if ($LASTEXITCODE -ne 0) { Write-Host "[postprovision] Upload failed -- fix and re-run: azd hooks run postprovision" -ForegroundColor Red; exit 1 }
        }
        & $python $ingest --ids-file $idsPath --build
        if ($LASTEXITCODE -ne 0) { Write-Host "[postprovision] Build failed -- see the error above." -ForegroundColor Red; exit 1 }
        Write-Host "[postprovision] Ingestion started. Poll: python scripts/hybrid_ingest.py --ids-file demo-ids.local.json --status" -ForegroundColor Green
    }
}

if ("$(Get-Out 'DEMO_CONFIGURE_FOUNDRY')" -ne 'false') {
    $python = if (Get-Out 'DEMO_PYTHON') { Get-Out 'DEMO_PYTHON' } else { 'python' }
    $setupArgs = @((Join-Path $repoRoot 'scripts' 'configure_foundry_iq.py'), '--ids-file', $idsPath)
    $knowledgeBase = Get-Out 'FOUNDRY_KNOWLEDGE_BASE'
    if ($knowledgeBase) { $setupArgs += @('--knowledge-base', $knowledgeBase) }
    & $python @setupArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Foundry IQ readiness/setup failed. Fix the reported stage and re-run postprovision; use DEMO_CONFIGURE_FOUNDRY=false only for foundation-only provisioning.'
    }
}

Write-Host "`n[postprovision] Done. Next:" -ForegroundColor Cyan
Write-Host "  Q&A: scripts\start_qa.ps1 -IdsFile $idsPath -Python <frontend-python>" -ForegroundColor Cyan
if (-not $corpus) {
    if ($uploader) {
        Write-Host "  Open $($uploader.url); the upload token is in $($uploader.accessFile)" -ForegroundColor Cyan
    } else {
        Write-Host "  python scripts/hybrid_ingest.py --ids-file demo-ids.local.json --split --upload --source-dir <your-pdfs>" -ForegroundColor Cyan
    }
    Write-Host "  python scripts/hybrid_ingest.py --ids-file demo-ids.local.json --build" -ForegroundColor Cyan
}
Write-Host "  Wire Copilot: docs/07 · Rehearse: python scripts/demo_walkthrough.py --ids-file demo-ids.local.json --script samples/walkthrough.example.json" -ForegroundColor Cyan
Write-Host "  Tear down:    azd down --purge" -ForegroundColor Cyan
