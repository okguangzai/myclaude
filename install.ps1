$ErrorActionPreference='Stop'
Push-Location $PSScriptRoot
try {
    if(-not(Test-Path '.venv\Scripts\python.exe')){& python -m venv .venv;if($LASTEXITCODE -ne 0){throw 'venv failed'}}
    $requirements='backend\requirements.txt'
    if(Test-Path 'backend\requirements.lock.txt'){$requirements='backend\requirements.lock.txt'}
    & .\.venv\Scripts\python.exe -m pip install -r $requirements
    if($LASTEXITCODE -ne 0){throw 'Python dependencies failed'}
    Push-Location frontend
    try {
        if(Test-Path package-lock.json){& npm.cmd ci}else{& npm.cmd install}
        if($LASTEXITCODE -ne 0){throw 'Node dependencies failed'}
        & npm.cmd run build
        if($LASTEXITCODE -ne 0){throw 'Frontend build failed'}
    } finally {Pop-Location}
    & .\.venv\Scripts\python.exe scripts\bootstrap_config.py
    if($LASTEXITCODE -ne 0){throw 'Configuration initialization failed'}
} finally {Pop-Location}
