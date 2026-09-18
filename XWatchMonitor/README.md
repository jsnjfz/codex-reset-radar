# X 重置监控（macOS 菜单栏应用）

一个常驻菜单栏的 macOS 应用，定期调用 [x-watch](../x-watch) 采集指定 X 博主的公开帖子，
用大模型判断是否在宣布**用量限额重置**，命中就发系统通知。点通知直接打开原帖。

典型监控对象是 `@thsottiaux`（OpenAI Codex，社区所说的 "Tibo reset"）。

## 功能

点菜单栏图标弹出的窗口，从上到下是：

1. **今日重置概率** —— 今天还没重置时，显示大模型算出的「今天还会重置」的概率、
   置信度和具体依据；**今天已经重置过就直接显示「今天已经重置过了」**，不再显示预测数字。
2. **状态行** —— 上次检查时间、间隔、判定开关、24 小时调用量。
3. **命中列表** —— 概率超过阈值的重置公告。
4. **最近帖子** —— 该账号最近的自有帖子（不只是命中），标注各自的判定概率或「未判定」。
5. **覆盖结论** —— 每个账号的覆盖状态，`limited` / `unknown` 标黄。

其它：

- 菜单栏常驻，可设 1～120 分钟检查间隔。reset 的端到端延迟 ≈ 这个间隔。
- 可指定监控账号；留空则用 `config.toml` 里所有启用账号。
- 用大模型给每条新帖打一个「是否重置公告」的概率，超过阈值才通知。
- 第一次保存时把当前命中设为基线，不会把历史公告一次性刷屏。
- 之后按 `post_id` 去重；同一条帖子只通知一次。
- 菜单里显示每个账号的**覆盖结论**；`limited` / `unknown` 会标黄 —— 表示本轮没能证明那段时间没遗漏。
- 「空跑」按钮只列候选帖子，不调模型、不消耗额度。
- 点击通知或列表条目打开原帖（只接受 https 链接）。

## 架构：为什么是两个语言

```
┌─────────────────────────┐   Process + JSON    ┌──────────────────────────┐
│  X 重置监控.app (Swift) │ ──────────────────► │  x-watch (Python)        │
│  菜单栏 / 定时 / 通知   │ ◄────────────────── │  采集 / 去重 / 判定       │
└─────────────────────────┘   stdout: JSON      └──────────────────────────┘
                              stderr: 日志
```

采集引擎不重写成 Swift —— x-watch 那边有 **200 个测试**覆盖去重、覆盖判定、429 同源退避、
提示词注入防护等等，还有对 FxTwitter 真实行为的实测结论（时间线非倒序、`count` 参数无效、
墓碑引用……）。这个 app 只做「监督 + 界面 + 通知」，采集逻辑保持单一来源。

两者之间是稳定的 JSON 契约（`x-watch/src/x_watch/report.py` ↔ `Sources/XWatchCore/Models.swift`），
`schema` 版本对不上时 app 会明确报错，而不是静默解析错。

## 构建和运行

```bash
cd /Users/happy/side/x-crawl/XWatchMonitor
./scripts/build-app.sh
open "/Users/happy/side/x-crawl/XWatchMonitor/dist/X 重置监控.app"
```

第一次使用：点顶部「reset」图标 → 设置… → 确认 x-watch 项目路径、填账号、调间隔和阈值 →
「保存并开始监控」，并允许 macOS 通知权限。

**要长期运行**，用 `./scripts/install.sh --login` 装到 `/Applications` 并开启开机启动
（走 macOS 13+ 的 `SMAppService`，会出现在「系统设置 → 通用 → 登录项」里，可随时自己关掉）。

## 今日概率是怎么算的（以及为什么不贵）

这和「判定单条帖子」是两件不同的事：

| | 判定 judge | 预测 forecast |
|---|---|---|
| 问题 | **这条帖子**是不是重置公告？ | **今天**还会不会重置？ |
| 输入 | 单条帖子正文 | 历史重置节奏 + 最近帖子 + 当天已过去多少 |
| 触发 | 每条新帖一次 | 见下 |

**关键的成本控制：预测不跟着轮询跑。** 间隔可以低到 1 分钟，但那样一天就是 1440 次调用。
所以按 `(日期, 账号)` 缓存，并且只在**输入真的变了**时重算：

- 当天还没算过 → 算一次
- 算过，但之后出现了更新的帖子 / 新的确认重置 → 重算
- 否则直接复用库里的结果，**零调用**（实测缓存命中 0.1 秒返回）

指纹刻意**不含当前时刻** —— 否则每分钟都会变，缓存就失效了。时间推进本身不足以重算。

另外：判定开关关闭时，预测**只读缓存、绝不发起新调用**。

实测输出（2026-09-17 深夜）：

```
今日预测：4%（置信度中）
  · 96% of day elapsed, no limit-related post
  · last reset only 5 days ago
  · launch week buzz but no capacity/limit hints
  · posts mention efficiency, not usage top-ups
```

提示词明确要求「平静的一天应该给 0.02~0.15，不要为了对冲就输出 0.5」——
这类公告本质上不可预测，一个诚实的模型应该给低数字。

## 前置条件

| 项 | 说明 |
|---|---|
| x-watch 项目 | 目录里要有 `config.toml`。app 用 `/usr/bin/python3` + `PYTHONPATH=<项目>/src` 调它，**不需要 pip install**（x-watch 运行时零第三方依赖）。 |
| Claude Code | 判定默认用 `backend = "claude_cli"`，走你已登录的 Claude Code 订阅额度，**不需要 API key**。 |

## 诊断

```bash
# 离线自检（不联网、不调模型）
swift run XWatchMonitorCheck --self-test

# 真跑一轮，走和 app 完全相同的 XWatchRunner
swift run XWatchMonitorCheck /Users/happy/side/x-crawl/x-watch thsottiaux
```

安装与开机启动：

```bash
./scripts/build-app.sh          # 构建到 dist/
./scripts/install.sh            # 装到 /Applications 并启动
./scripts/install.sh --login    # 顺便开启开机启动
```

**必须装到 `/Applications`**：`build-app.sh` 每次都会 `rm -rf` 掉 `dist/`，
而登录项记录的是 `.app` 的绝对路径 —— 指向 `dist/` 的登录项会在下次构建后失效。

诊断 CLI 用的是同一个 `XWatchRunner`，所以能把「终端里能跑、app 里不行」这类问题
定位到 PATH / 权限而不是逻辑。

### 为什么自检不是 `swift test`

这台机器只装了 CommandLineTools（没有 Xcode），`XCTest` 和 swift-testing 的 `Testing`
模块**都不可用** —— 实测 `swift test` 直接报 `no such module 'XCTest'`。所以 58 项断言放在
`XWatchMonitorCheck --self-test` 里真跑，而不是留一个跑不起来的 `Tests/` 目标。

## 两个从 Finder 启动才会踩到的坑

代码里已经处理，但值得知道：

1. **GUI app 不继承 shell PATH。** 从 Finder 或登录项启动时 PATH 通常只有
   `/usr/bin:/bin:/usr/sbin:/sbin`，判定后端要找的 `claude` 在 `/opt/homebrew/bin`，
   会直接找不到。`XWatchRunner` 显式把 Homebrew 路径拼进子进程 PATH。
2. **不能依赖 `python3` 在 PATH 里。** 固定用 `/usr/bin/python3`（macOS 自带）。

另外：子进程的 stdout/stderr 是**并发读**的。x-watch 的日志很长，等进程结束再读会把
64KB 管道写满而死锁。

## 不可信输入的处理

帖子正文和模型输出都是不可信文本，在这个 app 里：

- 通知标题/正文单行化、剥控制字符、限长（防终端转义序列之类）。
- 链接**只接受 https**；`javascript:`、`file:`、`http:` 一律不打开。
- 账号输入框会剥掉 `@`、URL 和非法字符。

真正的注入防护在 x-watch 那边（模型只当分类器、随机分隔符包裹正文、Claude Code 的工具
被四道锁封死）—— 详见 [x-watch 的 README](../x-watch/README.md#为什么注入攻击伤不到它)。

## 已知限制

- **Mac 休眠、关机或断网期间不会检查。** 唤醒后按间隔继续，不补跑错过的次数。
  所以「每 5 分钟」实际是「醒着的时候大约每 5 分钟」。要 7×24 盯着得放常开机器。
- **退出 app 就停止检查。** 这是菜单栏应用，不是后台守护进程。
- **概率是模型的判断，不是事实。** 阈值 0.7 是初始值，只在三条真实帖子上验证过。
  建议先把判定开关打开但观察几天，看准确率再依赖通知。
- **x-watch 不承诺一条不漏。** 覆盖结论为 `limited` / `unknown` 时界面会标黄。
- 同一时刻只有一个进程能写 x-watch 的数据库（文件锁）。你手动跑 `run-once` 时，
  app 这一轮会被识别为 **skipped**（保留上一轮数据、显示「本轮跳过」，不报错、不损坏数据）。
- **概率只是模型的估计。** 今日概率和单帖判定都是模型输出，不是事实。

## 已实测

- Swift → Python → FxTwitter 端到端跑通：`XWatchMonitorCheck` 输出契约 `schema=2`、
  三个账号覆盖结论 `overlap_observed`、命中 `0.95 @thsottiaux "Reset all propagated. Sweet dreams."`。
- `--judge` / `--no-judge` 覆盖开关生效（config 里 `enabled=false` 时也能强制判定）。
- 今日概率链路跑通：真实算出 4%，缓存命中 0.1 秒零调用。
- 锁冲突被正确识别为 skipped 而不是失败。
- 58 项离线自检通过（含契约版本对齐检查 —— 它在开发中真的抓到过一次 schema 漂移）。
- `.app` 打包 + adhoc 签名成功。

- **开机启动已开启并确认**：`/Applications/X 重置监控.app`，`SMAppService` 状态 `enabled`。
  状态写在 `~/Library/Application Support/XWatchMonitor/login-status.txt`，
  用 `./scripts/install.sh` 可随时复核。

**未实测**：菜单栏 UI 的实际观感、通知实际弹出、重启后自启动是否真的拉起、长时间运行的
稳定性 —— 这些需要你在图形界面里点一遍 / 重启一次才能确认。
