param(
    [string]$Output = "release\PsychoVertexMaster.zip"
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$destination = Join-Path $root $Output
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination }
$excludeDirectories = @('.git', 'release', 'backups', 'downloads', 'denoisers', '__pycache__')
$excludeExtensions = @('.pyc', '.pyo')
$files = Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($root.Length).TrimStart('\', '/')
    $parts = $relative -split '[\\/]'
    -not ($parts | Where-Object { $excludeDirectories -contains $_ }) -and
    -not ($excludeExtensions -contains $_.Extension)
}
$staging = Join-Path $env:TEMP ("pvm_release_" + [guid]::NewGuid().ToString('N'))
$addon = Join-Path $staging "PsychoVertexMaster"
try {
    New-Item -ItemType Directory -Force -Path $addon | Out-Null
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\', '/')
        $target = Join-Path $addon $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }
    Compress-Archive -LiteralPath $addon -DestinationPath $destination -CompressionLevel Optimal
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
Write-Host "Created $destination"
