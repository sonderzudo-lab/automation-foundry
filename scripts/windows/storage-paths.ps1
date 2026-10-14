# Shared by scheduled tasks: use the application's dotenv parser and environment
# precedence, and resolve relative paths from the repository, never the task cwd.
function Get-FoundryStorageRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryRoot,
        [string]$PythonExecutable = (Join-Path $RepositoryRoot '.venv\Scripts\python.exe')
    )

    if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw 'Project Python executable not found; run the project setup first.'
    }
    Push-Location -LiteralPath $RepositoryRoot
    try {
        # Never emit a settings dump or validation exception: they may contain secrets.
        $code = @'
import json
import sys
from pathlib import Path
try:
    from src.core.config import get_settings
    print(json.dumps(str(Path(get_settings().storage_root).resolve())))
except Exception:
    sys.exit(2)
'@
        $result = & $PythonExecutable -c $code 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $result) {
            throw 'Storage configuration could not be loaded; check the project .env.'
        }
        return ($result | ConvertFrom-Json)
    } finally {
        Pop-Location
    }
}
