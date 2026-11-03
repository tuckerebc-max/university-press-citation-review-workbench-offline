param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$EnableProvider
)

$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$python = if (Test-Path -LiteralPath $bundledPython) { $bundledPython } else { (Get-Command python -ErrorAction Stop).Source }
if ($EnableProvider -and -not $env:LUNAROUTE_API_KEY) {
    $env:LUNAROUTE_API_KEY = [Environment]::GetEnvironmentVariable('LUNAROUTE_API_KEY', 'User')
}
$env:PYTHONPATH = $appRoot
$arguments = @('-X', 'utf8', '-m', 'upress_workbench.server', '--port', [string]$Port)
if ($NoBrowser) { $arguments += '--no-browser' }
if ($EnableProvider) { $arguments += '--enable-provider' }
& $python @arguments
