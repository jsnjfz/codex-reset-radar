# codex-reset-radar

盯着指定 X 博主的公开帖子，用大模型判断有没有在宣布**用量限额重置**，命中就弹 macOS 通知。

典型监控对象是 [@thsottiaux](https://x.com/thsottiaux)（Thibault "Tibo" Sottiaux，OpenAI
Head of Core Products / Codex）—— 社区所说的 "Tibo reset" 就是他发的。

> **目录名与仓库名不同**：GitHub 上叫 `codex-reset-radar`，clone 下来的目录名随你。
> 本机开发时它在 `~/side/x-crawl/`，app 的默认项目路径就指向那里 —— 换位置的话在
> 设置面板里改「x-watch 项目」即可，不用改代码。

## 两个组件

| 目录 | 是什么 | 语言 |
|---|---|---|
| [`x-watch/`](x-watch/) | 采集引擎：抓取、去重、覆盖判定、大模型判定、今日概率预测、Markdown 归档 | Python（运行时零第三方依赖） |
| [`XWatchMonitor/`](XWatchMonitor/) | macOS 菜单栏应用：定时调度、界面、系统通知、开机启动 | Swift（SwiftUI + SPM） |

```
┌─────────────────────────┐   Process + JSON    ┌──────────────────────────┐
│  X 重置监控.app (Swift) │ ──────────────────► │  x-watch (Python)        │
│  菜单栏 / 定时 / 通知   │ ◄────────────────── │  采集 / 判定 / 预测       │
└─────────────────────────┘   stdout: JSON      └──────────────────────────┘
                              stderr: 日志
```

**为什么是两个语言**：采集引擎有 200+ 个测试覆盖去重、覆盖判定、429 同源退避、提示词注入
防护，还有对上游真实行为的实测结论。app 只做「监督 + 界面 + 通知」，不重写采集逻辑。
两者之间是带版本号的 JSON 契约，版本对不上时 app 会明确报错而不是静默解析错。

## 快速开始

只想在命令行用采集引擎：

```sh
cd x-watch
cp config.example.toml config.toml   # 改成你关心的账号
PYTHONPATH=src python3 -m x_watch --config config.toml doctor
PYTHONPATH=src python3 -m x_watch --config config.toml run-once
```

想要常驻的菜单栏 app：

```sh
cd XWatchMonitor
./scripts/build-app.sh
./scripts/install.sh --login    # 装到 /Applications 并开启开机启动
```

细节见 [x-watch/README.md](x-watch/README.md) 和
[XWatchMonitor/README.md](XWatchMonitor/README.md)。

## 数据从哪来

[FxEmbed](https://github.com/FxEmbed/FxEmbed)（原 FxTwitter）的公开 JSON 接口 —— MIT 开源、
跑在 Cloudflare Workers 上的第三方服务，不需要 X 的 API Key 或 Cookie。

**它是志愿者维护的免费服务，没有 SLA、没有可用性承诺。** 文档给出的配额是每 IP 每分钟
1000 次；本项目默认 20 分钟一轮、单账号，约 72 次/天，占配额的 0.005%。量大时它自己的文档
建议自建实例 —— 项目里数据源是抽象成 provider 的，换掉不影响上层任何一层。

## 判定用什么模型

默认调**本机已登录的 Claude Code**（`claude -p`），走订阅额度，不需要 API key。
也可以切到 Anthropic SDK（`backend = "api"`，需要 `ANTHROPIC_API_KEY`，输出有约束解码）。

模型在这里**只是分类器**：没有工具、输出被约束成固定 JSON。帖子正文来自公开时间线，
任何人都能在里面写「忽略上面的规则」，所以全链路按不可信数据处理 —— 随机分隔符包裹、
指令走 system 数据走 user turn、模型输出再转义。

`claude_cli` 后端还多一层风险：Claude Code 本身是带工具的 agent。实测只传
`--allowed-tools ''` **挡不住 Bash**（它真的执行了 `id`），所以用了四道锁封死。
详见 [x-watch/README.md](x-watch/README.md#claude_cli-后端多一层风险必须显式封死)。

## 设计依据

实现前先做了一轮真实接口验证，结论写在
[`x-watch/docs/source-validation.md`](x-watch/docs/source-validation.md)。有三条直接
推翻了方案文档里的假设：

- **`type` 字段不能用来判断帖子类型** —— 实测单页 36 条全是 `status`，类型只能从
  `reposted_by` / `replying_to` / `quote` 的结构推导。
- **时间线不是严格倒序** —— 上游按对话聚合返回，所以不能「遇到旧帖就停止翻页」。
- **`count` 参数被校验但不生效** —— 请求 5 条返回 36 条，不能用「返回条数 < page_size」
  推断历史结束。

原始需求与设计约束见 [`X博主帖子自动采集方案.md`](X博主帖子自动采集方案.md)。实现基本
遵循它，几处有意偏离（零依赖、判定/预测/翻译、菜单栏 app 而非系统定时任务）在各自
README 的「与方案的差异」里写明了。

## 诚实的边界

- **不承诺一条不漏。** 覆盖结论为 `limited` / `unknown` 时表示本轮没能证明那段时间没有
  遗漏，界面和每日索引都会标出来。
- **概率是模型的判断，不是事实。** 单帖判定和今日概率都是模型输出。
- **Mac 休眠、关机或断网期间不会检查。** 唤醒后按间隔继续，不补跑错过的次数。
- **退出 app 就停止检查。** 这是菜单栏应用，不是后台守护进程。
- 第三方免费服务的长期可用性无法保证，本项目不对此做任何背书。

## 使用条件

只处理获准访问的公开内容，归档保留作者与原帖链接。公开转载或商业再分发不属于本项目的
默认授权范围。主路线不需要保存 X 密码、Cookie 或官方 API Key。
