# x-watch 单次采集入口（Windows）。计划任务只调用这个脚本。
#
# 要点（方案 §11.2）：
# - 用绝对路径的虚拟环境解释器，不依赖手动激活 venv 或某个终端里设过的变量。
# - 显式设置工作目录。
# - 原样保留程序退出码：0 正常 / 1 partial-failed-导出失败 / 2 配置错误。

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectDir

$ConfigPath = if ($env:X_WATCH_CONFIG) { $env:X_WATCH_CONFIG } else { Join-Path $ProjectDir 'config.toml' }

# 优先用项目自带虚拟环境。本项目运行时零第三方依赖，没有 venv 也能跑。
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    $Python = $VenvPython
} elseif ($env:X_WATCH_PYTHON -and (Test-Path -LiteralPath $env:X_WATCH_PYTHON)) {
    $Python = $env:X_WATCH_PYTHON
} else {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $Python = $py.Source
        $PyArgs = @('-3')
    } else {
        $python3 = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python3) {
            Write-Error '找不到 Python；请安装 Python 3.9+ 或设置 X_WATCH_PYTHON'
            exit 2
        }
        $Python = $python3.Source
    }
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "配置文件不存在：$ConfigPath（可从 config.example.toml 复制）"
    exit 2
}

$env:PYTHONPATH = if ($env:PYTHONPATH) {
    (Join-Path $ProjectDir 'src') + [System.IO.Path]::PathSeparator + $env:PYTHONPATH
} else {
    Join-Path $ProjectDir 'src'
}
# 避免中文输出在计划任务的非 UTF-8 控制台里变成乱码
$env:PYTHONIOENCODING = 'utf-8'

$allArgs = @()
if ($PyArgs) { $allArgs += $PyArgs }
$allArgs += @('-m', 'x_watch', '--config', $ConfigPath, 'run-once')

& $Python @allArgs
$status = $LASTEXITCODE

# 退出码 0 只代表本轮按策略完成，不代表没有遗漏。覆盖情况用 status 命令看。
exit $status
