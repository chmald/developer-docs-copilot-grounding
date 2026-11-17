<#
.SYNOPSIS
Deploy a Content Understanding resource in a documented supported region.
.DESCRIPTION
The default foundation Foundry region might not support the Content Understanding
GA API. This creates a separate S0 AIServices resource/project in South Central US
and grants the existing Search managed identity Cognitive Services User.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$IdsFile,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{1,53}[a-zA-Z0-9]$')]
    [string]$AccountName = 'aif-ddmcp-dellpdf-cu-scus',
    [ValidateSet('australiaeast','canadacentral','eastus','eastus2','japaneast',
        'southcentralus','southeastasia','swedencentral','uksouth','westeurope',
        'westus','westus3')]
    [string]$Location = 'southcentralus'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$idsPath = [IO.Path]::GetFullPath($IdsFile)
$ids = Get-Content -LiteralPath $idsPath -Raw | ConvertFrom-Json
$context = az account show -o json --only-show-errors | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $ids.tenantId -or -not $ids.subscriptionId -or
    $context.tenantId -ne $ids.tenantId -or $context.id -ne $ids.subscriptionId) {
    throw 'Azure CLI target does not match the IDs file. Authenticate to its tenant/subscription first.'
}
if (-not $ids.resourceGroup -or -not $ids.searchService) {
    throw 'The IDs file must contain resourceGroup and searchService from the foundation deployment.'
}
$searchPrincipalId = az search service show --name $ids.searchService --resource-group $ids.resourceGroup `
    --subscription $ids.subscriptionId --query identity.principalId -o tsv --only-show-errors
if ($LASTEXITCODE -ne 0 -or -not $searchPrincipalId) {
    throw 'Could not resolve the live Azure AI Search managed identity.'
}

$deploymentName = 'content-understanding-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$raw = az deployment group create --name $deploymentName --resource-group $ids.resourceGroup `
    --subscription $ids.subscriptionId --template-file (Join-Path $repo 'infra\content-understanding.bicep') `
    --parameters accountName=$AccountName location=$Location searchPrincipalId=$searchPrincipalId `
    --query properties.outputs -o json --only-show-errors
if ($LASTEXITCODE -ne 0) { throw 'Content Understanding resource deployment failed.' }
$output = $raw | ConvertFrom-Json
$ids | Add-Member -NotePropertyName searchPrincipalId -NotePropertyValue $searchPrincipalId -Force
$ids | Add-Member -NotePropertyName contentUnderstandingResource -NotePropertyValue $output.accountName.value -Force
$ids | Add-Member -NotePropertyName contentUnderstandingSubdomainUrl -NotePropertyValue $output.subdomainUrl.value -Force
$ids | Add-Member -NotePropertyName contentUnderstandingLocation -NotePropertyValue $Location -Force
$ids | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $idsPath -Encoding utf8
Write-Host "[content-understanding] Resource deployed in $Location; the Search identity has Cognitive Services User."
[pscustomobject]@{
    accountName = $output.accountName.value
    subdomainUrl = $output.subdomainUrl.value
    projectName = $output.projectName.value
    location = $Location
}
