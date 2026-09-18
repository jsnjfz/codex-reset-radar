# Windows 计划任务注册 / 检查 / 删除。
#
# 用法：
#   .\scripts\register_task.ps1 -Action Show        显示将要创建的任务（不做改动）
#   .\scripts\register_task.ps1 -Action Install     创建任务
#   .\scripts\register_task.ps1 -Action Status      查看任务状态与上次运行结果
#   .\scripts\register_task.ps1 -Action Uninstall   删除任务
#
# 注册前一定先看 Show 的输出，确认任务名、命令、运行身份和间隔（方案 §11.2）。
#
# 已知限制：电脑休眠、关机或断网期间不会采集。下面用 StartWhenAvailable 让错过的
# 触发在恢复后补跑一次，但不会把错过的每一次都补上。

[CmdletBinding()]
param(
    [ValidateSet('Show', 'Install', 'Status', 'Uninstall')]
    [string]$Action = 'Show',

    [string]$TaskName = 'x-watch-run-once'
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $PSScriptRoot
$ConfigPath = if ($env:X_WATCH_CONFIG) { $env:X_WATCH_CONFIG } else { Join-Path $ProjectDir 'config.toml' }
$Runner = Join-Path $ProjectDir 'scripts\run_once.ps1'

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "配置文件不存在：$ConfigPath"
    exit 2
}

# 间隔从 config.toml 读取，避免"改了配置却没改系统任务"（方案 §8）
$env:PYTHONPATH = Join-Path $ProjectDir 'src'
$IntervalMinutes = [int](& python -c @"
import sys
from x_watch.toml_compat import load_toml
data = load_toml(r'$ConfigPath')
print(int(data.get('collection', {}).get('interval_minutes', 60)))
"@)
if ($LASTEXITCODE -ne 0) {
    Write-Error '无法从 config.toml 读取 interval_minutes'
    exit 2
}

$PwshPath = (Get-Process -Id $PID).Path   # 当前 PowerShell 宿主的绝对路径
$Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`""

function Show-Plan {
    Write-Host "任务名            : $TaskName"
    Write-Host "执行程序          : $PwshPath"
    Write-Host "参数              : $Arguments"
    Write-Host "工作目录          : $ProjectDir"
    Write-Host "配置文件          : $ConfigPath"
    Write-Host "运行身份          : $env:USERDOMAIN\$env:USERNAME（仅在该用户登录时运行）"
    Write-Host "触发间隔          : 每 $IntervalMinutes 分钟"
    Write-Host "日志目录          : $(Join-Path $ProjectDir 'logs')"
    Write-Host ''
    Write-Host '任务设置：'
    Write-Host '  - MultipleInstances = IgnoreNew  上次任务没结束就不启动新实例'
    Write-Host '  - StartWhenAvailable = true      错过触发后在恢复时补跑一次'
    Write-Host '  - ExecutionTimeLimit = 1 小时    卡死的任务会被终止'
    Write-Host '  - RestartOnIdle / 唤醒计算机     均不启用；不会为采集唤醒机器'
    Write-Host ''
    Write-Host '注意：x-watch 自身还有单实例文件锁，手动运行和计划任务不会同时写库。'
    Write-Host '注意：计划任务的环境变量（含代理）可能和你的终端不同，注册后请再跑一次 doctor 核对。'
}

switch ($Action) {
    'Show' {
        Show-Plan
        Write-Host ''
        Write-Host '===== 以上任务尚未创建。使用 -Action Install 才会真正注册。 ====='
    }

    'Install' {
        Show-Plan
        Write-Host ''
        $answer = Read-Host '确认注册以上任务？输入 yes 继续'
        if ($answer -ne 'yes') {
            Write-Host '已取消，未做任何改动。'
            exit 1
        }

        Write-Host '注册前自检（doctor --no-network）...'
        & python -m x_watch --config $ConfigPath doctor --no-network
        if ($LASTEXITCODE -ne 0) {
            Write-Error 'doctor 未通过，已放弃注册。先修好上面的问题。'
            exit 2
        }

        New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir 'logs') | Out-Null

        $action = New-ScheduledTaskAction -Execute $PwshPath -Argument $Arguments -WorkingDirectory $ProjectDir
        # 开机后 5 分钟先跑一次，再按间隔重复；重复周期设为 3650 天等价于"一直重复"
        $trigger = New-ScheduledTaskTrigger -AtLogOn -RandomDelay (New-TimeSpan -Minutes 5)
        $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition

        $settings = New-ScheduledTaskSettingsSet `
            -MultipleInstances IgnoreNew `
            -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -DontStopIfGoingOnBatteries `
            -AllowStartIfOnBatteries

        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Description 'x-watch：定期收集指定 X 博主的公开帖子' -Force | Out-Null

        Write-Host ''
        Write-Host "已注册任务 $TaskName。用 -Action Status 查看状态。"
    }

    'Status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Host "未注册任务 $TaskName"
            exit 0
        }
        Write-Host "任务名   : $TaskName"
        Write-Host "状态     : $($task.State)"
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "上次运行 : $($info.LastRunTime)"
        Write-Host "上次结果 : $($info.LastTaskResult)  (0 正常 / 1 partial 或导出失败 / 2 配置错误)"
        Write-Host "下次运行 : $($info.NextRunTime)"
        Write-Host ''
        Write-Host '程序自身的运行记录与覆盖情况：'
        & python -m x_watch --config $ConfigPath status
    }

    'Uninstall' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "已删除任务 $TaskName"
        } else {
            Write-Host "未注册任务 $TaskName，无需删除"
        }
        Write-Host "数据库、Markdown 输出和日志都保留在 $ProjectDir，本脚本不会删除它们。"
    }
}
