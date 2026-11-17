<#
.SYNOPSIS
Build and deploy the private-storage browser uploader without local Docker.
.DESCRIPTION
Requires the foundation deployment and a dedicated delegated Container Apps subnet.
Storage public access, the existing private endpoint, and network rules are not changed.
The upload token is stored as a Container App secret and in a private local access file,
never printed or added to the IDs file. Reuses that file on subsequent deployments.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$IdsFile,
    [Parameter(Mandatory)][string]$SubnetId,
    [string]$AccessFile = '',
    [ValidatePattern('^$|^pdf-uploader:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')][string]$ImageTag = '',
    [ValidateRange(1, 134217728)][int]$MaxUploadBytes = 33554432
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$ids = Get-Content -LiteralPath $IdsFile -Raw | ConvertFrom-Json
$context = az account show -o json --only-show-errors | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $ids.tenantId -or -not $ids.subscriptionId -or
    $context.tenantId -ne $ids.tenantId -or $context.id -ne $ids.subscriptionId) {
    throw 'Azure CLI target does not match the IDs file. Authenticate to its tenant/subscription first.'
}
if ($ids.environment -notmatch '^[a-z0-9]{1,12}$') { throw 'Invalid environment name in IDs file.' }
if ($SubnetId -notmatch '^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft.Network/virtualNetworks/([^/]+)/subnets/([^/]+)$' -or
    $Matches[1] -ne $ids.subscriptionId) { throw 'SubnetId must be in the IDs file subscription.' }
$networkGroup, $vnetName = $Matches[2], $Matches[3]
$subnet = az network vnet subnet show --ids $SubnetId --subscription $ids.subscriptionId -o json --only-show-errors | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not read the Container Apps subnet.' }
if ('Microsoft.App/environments' -notin @($subnet.delegations.serviceName)) {
    throw 'The dedicated subnet must be delegated to Microsoft.App/environments.'
}
if (-not $subnet.addressPrefix -or [int]($subnet.addressPrefix -split '/')[1] -gt 27) {
    throw 'Use an IPv4 /27-or-larger dedicated Container Apps subnet.'
}
$location = az network vnet show --resource-group $networkGroup --name $vnetName --subscription $ids.subscriptionId --query location -o tsv --only-show-errors
if ($LASTEXITCODE -ne 0 -or -not $location) { throw 'Could not resolve the VNet region.' }
if (-not $AccessFile) {
    $AccessFile = Join-Path (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'DeveloperDocsUploader') "$($ids.environment).local.json"
}
$AccessFile = [IO.Path]::GetFullPath($AccessFile)
if (Test-Path -LiteralPath $AccessFile) {
    $access = Get-Content -LiteralPath $AccessFile -Raw | ConvertFrom-Json
    if ($access.subscriptionId -ne $ids.subscriptionId -or $access.environment -ne $ids.environment -or
        $access.token.Length -lt 32) { throw 'Existing access file does not match this deployment.' }
    $token = $access.token
} else {
    $token = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
    $null = New-Item -ItemType Directory -Force -Path (Split-Path -Parent $AccessFile)
    [ordered]@{
        subscriptionId = $ids.subscriptionId; environment = $ids.environment; token = $token
    } | ConvertTo-Json | Set-Content -LiteralPath $AccessFile -Encoding utf8
}
$parameterFile = [IO.Path]::GetTempFileName()
$params = @{
    environment = @{ value = $ids.environment }
    location = @{ value = "$location".Trim() }
    infrastructureSubnetId = @{ value = $SubnetId }
    storageAccountName = @{ value = $ids.storageAccount }
    rawContainer = @{ value = $(if ($ids.rawContainer) { $ids.rawContainer } else { 'raw' }) }
    uploadToken = @{ value = $token }
    maxUploadBytes = @{ value = $MaxUploadBytes }
    contentUnderstandingEnabled = @{ value = ($ids.hybridContentUnderstandingEnabled -ne $false) }
}
function Deploy-Uploader([string]$Name) {
    @{ parameters = $params } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $parameterFile -Encoding utf8
    $raw = az deployment group create --name $Name --resource-group $ids.resourceGroup `
        --subscription $ids.subscriptionId --template-file (Join-Path $repo 'infra\uploader.bicep') `
        --parameters "@$parameterFile" --query properties.outputs -o json --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "Uploader deployment '$Name' failed." }
    return ($raw | ConvertFrom-Json)
}
try {
    Write-Host '[uploader] Provisioning identity, container-scoped RBAC, registry, and VNet environment...'
    $bootstrap = Deploy-Uploader 'uploader-foundation'
    if ($ImageTag) {
        $tag = $ImageTag
        az acr repository show --subscription $ids.subscriptionId --name $bootstrap.registryName.value `
            --image $tag --output none --only-show-errors
        if ($LASTEXITCODE -ne 0) { throw 'The requested uploader image does not exist in this registry.' }
        Write-Host "[uploader] Reusing verified registry image $tag."
    } else {
        $tag = 'pdf-uploader:' + (Get-Date -AsUTC -Format 'yyyyMMddHHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
        Write-Host "[uploader] Building $tag remotely; only the uploader build context is sent."
        az acr build --subscription $ids.subscriptionId --registry $bootstrap.registryName.value `
            --platform linux/amd64 --image $tag --file (Join-Path $repo 'uploader' 'Dockerfile') (Join-Path $repo 'uploader') `
            --no-logs --output none --only-show-errors
        if ($LASTEXITCODE -ne 0) { throw 'Remote image build failed; the running app was not changed.' }
    }
    $params.imageTag = @{ value = $tag }
    Write-Host '[uploader] Deploying the authenticated upload app...'
    $output = Deploy-Uploader 'uploader-app'
    $safe = [ordered]@{
        url = $output.url.value
        containerAppName = $output.containerAppName.value
        environmentName = $output.environmentName.value
        registryName = $output.registryName.value
        subnetId = $SubnetId
    }
    $access = [ordered]@{
        subscriptionId = $ids.subscriptionId; environment = $ids.environment
        token = $token; url = $safe.url
    }
    $access | ConvertTo-Json | Set-Content -LiteralPath $AccessFile -Encoding utf8
    $ids | Add-Member -NotePropertyName uploader -NotePropertyValue $safe -Force
    $ids | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $IdsFile -Encoding utf8
    Write-Host "[uploader] URL: $($safe.url)"
    Write-Host "[uploader] Private access file (contains the token): $AccessFile"
    [pscustomobject]@{ url = $safe.url; accessFile = $AccessFile }
} finally {
    Remove-Item -LiteralPath $parameterFile -Force
}
