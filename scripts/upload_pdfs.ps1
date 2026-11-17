<#
.SYNOPSIS
Send local PDFs to the authenticated Container Apps uploader, not directly to Storage.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$AccessFile,
    [string]$SourceDir = '',
    [switch]$AllowLarge,
    [switch]$StatusOnly
)
$ErrorActionPreference = 'Stop'
$access = Get-Content -LiteralPath $AccessFile -Raw | ConvertFrom-Json
$uri = [Uri]$access.url
if ($uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') {
    throw 'The access file URL must be an HTTPS origin without credentials, query, or path.'
}
if (-not $access.token -or $access.token.Length -lt 32) { throw 'The access file has no valid upload token.' }
$headers = @{ Authorization = "Bearer $($access.token)" }
$status = Invoke-RestMethod -Uri "$($access.url)/api/status" -Headers $headers -TimeoutSec 120
if (-not $status.storageReady) { throw 'The uploader cannot reach private Storage.' }
$status | ConvertTo-Json -Depth 5 | Write-Host
if (-not $StatusOnly) {
    if (-not $SourceDir -or -not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw 'Supply a valid SourceDir, or use StatusOnly.'
    }
    $files = @(Get-ChildItem -LiteralPath $SourceDir -File -Filter '*.pdf' | Sort-Object Name)
    if (-not $files.Count) { throw 'SourceDir contains no PDFs.' }
    foreach ($file in $files) {
        if ($file.Length -gt $status.maxBytes) { throw "$($file.Name) exceeds the uploader size limit." }
        $name = [Uri]::EscapeDataString($file.Name)
        $allow = if ($AllowLarge) { 'true' } else { 'false' }
        $result = Invoke-RestMethod -Method Post -Uri "$($access.url)/api/upload?filename=$name&allow_large=$allow" `
            -Headers $headers -ContentType application/pdf -InFile $file.FullName -TimeoutSec 230
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($result.sha256 -ne $hash -or $result.bytes -ne $file.Length) {
            throw "Uploader integrity verification failed for $($file.Name)."
        }
        $result | Select-Object filename, blob, pages, bytes, sha256 | Format-Table -AutoSize | Out-Host
    }
}
Invoke-RestMethod -Uri "$($access.url)/api/files" -Headers $headers -TimeoutSec 120 | ConvertTo-Json -Depth 10
