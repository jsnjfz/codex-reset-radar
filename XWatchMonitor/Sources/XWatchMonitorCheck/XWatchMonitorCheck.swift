import Foundation
import XWatchCore

/// 诊断 CLI：在终端里跑一次，确认 app 用的那条链路是通的。
///
/// 用法：
///   swift run XWatchMonitorCheck [项目路径] [账号...]
///
/// 它走的是和菜单栏 app **完全相同**的 XWatchRunner，所以能把
/// 「app 里不工作但终端里能跑」这类问题定位到 PATH / 权限而不是逻辑。
@main
struct XWatchMonitorCheck {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())

        if args.contains("--self-test") {
            print("离线自检（不联网、不调模型）")
            exit(SelfTest.run())
        }

        let projectPath = args.first.map { path -> String in
            args.removeFirst()
            return path
        } ?? MonitorSettings.defaultProjectPath
        let handles = args

        print("项目路径：\(projectPath)")
        print("账号：\(handles.isEmpty ? "（config.toml 里全部启用账号）" : handles.joined(separator: ", "))")
        print("Python：\(XWatchRunner.systemPython)")
        print("补充 PATH：\(XWatchRunner.extraPathEntries.joined(separator: ":"))")
        print("")

        let runner = XWatchRunner(projectPath: projectPath)
        do {
            try runner.validate()
            print("✓ 项目结构与 config.toml 就位：\(runner.configPath)")
        } catch {
            print("✗ \(error.localizedDescription)")
            exit(2)
        }

        print("正在跑一轮采集（会真的请求上游）…")
        do {
            let report = try await runner.runTick(handles: handles)
            print("✓ 契约 schema=\(report.schema)，ok=\(report.ok.map(String.init) ?? "-")")
            if report.wasSkipped {
                print("  本轮被跳过：\(report.skippedReason ?? "-")")
                print("  （菜单栏 app 可能正在跑同一轮，这是单实例锁的正常行为）")
                exit(0)
            }
            if let forecast = report.forecasts?.first {
                if forecast.hasResetToday {
                    print("  今日预测：已经重置过了")
                } else {
                    print("  今日预测：\(forecast.percentText)（\(forecast.confidenceLabel)）"
                          + (forecast.cached == true ? " [缓存]" : ""))
                    for signal in forecast.signals { print("    · \(signal)") }
                }
            }
            print("  最近帖子 \(report.recentPosts?.count ?? 0) 条")
            if let collection = report.collection {
                print("  采集：新帖 \(collection.newPosts ?? 0)，取页 \(collection.pagesFetched ?? 0)，状态 \(collection.worstStatus ?? "-")")
            }
            if let judge = report.judge {
                print("  判定：\(judge.enabled ? "已开启" : "未开启")，后端 \(judge.backend ?? "-")，模型 \(judge.model ?? "-")")
            }
            if let run = report.judgeRun, let judged = run.judged {
                print("  本轮判定 \(judged) 条，失败 \(run.failed ?? 0) 条，命中 \(run.triggered ?? 0) 条")
            }
            for account in report.accounts {
                print("  @\(account.handle) 覆盖=\(account.coverageStatus ?? "-")"
                      + (account.coverageIsProven ? "" : "（未证明完整）"))
            }
            print("  当前命中 \(report.resets.count) 条：")
            for verdict in report.resets.prefix(5) {
                print(String(format: "    %.2f  @%@  %@",
                             verdict.probability,
                             verdict.authorHandle,
                             NotificationTextPreview.oneLine(verdict.text, limit: 80)))
            }
            for error in report.errors ?? [] {
                print("  ! \(error)")
            }
            exit(report.ok == false ? 1 : 0)
        } catch {
            print("✗ \(error.localizedDescription)")
            exit(1)
        }
    }
}

/// 和 app 里通知文案相同的单行化处理，方便对照。
enum NotificationTextPreview {
    static func oneLine(_ raw: String, limit: Int) -> String {
        let collapsed = raw
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        if collapsed.count <= limit { return collapsed }
        return String(collapsed.prefix(limit - 1)) + "…"
    }
}
