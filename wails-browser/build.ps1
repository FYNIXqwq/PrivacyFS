param([switch]$Offline)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$savedBuildEnv = @{}
foreach ($key in @('GOCACHE','GOARCH','CGO_ENABLED','GOTOOLCHAIN','GOPROXY','GOSUMDB')) { $savedBuildEnv[$key] = [Environment]::GetEnvironmentVariable($key,'Process') }
try {
    if (-not $env:GOCACHE) { $env:GOCACHE = Join-Path $projectRoot '.cache\go-build' }
    $env:GOARCH = 'amd64'
    $env:CGO_ENABLED = '0'
    $env:GOTOOLCHAIN = 'local'
    if ($Offline) { $env:GOPROXY='off'; $env:GOSUMDB='off' }
    Push-Location -LiteralPath $projectRoot
    try {
        & wails3 generate syso -arch amd64 -icon build/windows/icon.ico -manifest build/windows/wails.exe.manifest -info build/windows/info.json -out wails_windows_amd64.syso
        if ($LASTEXITCODE -ne 0) { throw 'Windows resource generation failed' }
        & go build -tags production -trimpath -buildvcs=false '-ldflags=-s -w -H windowsgui' -o bin/PrivacyFS-Browser.exe .
        if ($LASTEXITCODE -ne 0) { throw 'Go build failed' }
        Write-Output (Join-Path $projectRoot 'bin\PrivacyFS-Browser.exe')
    } finally { Pop-Location }
} finally {
    foreach ($key in $savedBuildEnv.Keys) { [Environment]::SetEnvironmentVariable($key,$savedBuildEnv[$key],'Process') }
}
