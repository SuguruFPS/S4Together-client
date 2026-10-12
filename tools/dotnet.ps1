param()
$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE = 'true'
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
$env:MSBuildEnableWorkloadResolver = 'false'
$sdk = Join-Path $PSScriptRoot '..\..\.dotnet\dotnet.exe'
if (Test-Path -LiteralPath $sdk) { $env:DOTNET_HOST_PATH = $sdk; & $sdk @args } else { & dotnet @args }
exit $LASTEXITCODE
