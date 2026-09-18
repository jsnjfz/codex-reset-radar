import Foundation
import XWatchCore

/// 离线自检。
///
/// 这台机器只装了 CommandLineTools（没有 Xcode），`XCTest` 和 `Testing` 两个模块都不可用
/// —— 参照项目的 `Tests/` 目录也因此是空的（`swift test` 报 "no tests found"）。
/// 所以断言放在这里，用 `swift run XWatchMonitorCheck --self-test` 真跑，
/// 而不是留一个跑不起来的测试目标。
enum SelfTest {
    private static var failures: [String] = []
    private static var checks = 0

    static func run() -> Int32 {
        failures = []
        checks = 0

        settingsParsing()
        urlSafety()
        coverageHonesty()
        languageSwitching()
        jsonLineExtraction()
        reportDecoding()
        forecastAndPosts()
        runnerConfiguration()

        print("")
        if failures.isEmpty {
            print("✓ 自检通过：\(checks) 项")
            return 0
        }
        print("✗ 自检失败 \(failures.count)/\(checks) 项：")
        for failure in failures { print("   - \(failure)") }
        return 1
    }

    // MARK: - 断言

    private static func expect(_ condition: Bool, _ label: String) {
        checks += 1
        if !condition { failures.append(label) }
    }

    private static func expectEqual<T: Equatable>(_ lhs: T, _ rhs: T, _ label: String) {
        checks += 1
        if lhs != rhs { failures.append("\(label)（得到 \(lhs)，期望 \(rhs)）") }
    }

    // MARK: - 用例

    private static func settingsParsing() {
        print("· 账号解析")
        var settings = MonitorSettings()

        settings.handlesText = "thsottiaux, @bcherny，simonw 、 dotey"
        expectEqual(settings.handles, ["thsottiaux", "bcherny", "simonw", "dotey"],
                    "混合分隔符（逗号/中文逗号/顿号/空格）")

        settings.handlesText = "@Tibo, https://x.com/simonw"
        expectEqual(settings.handles, ["tibo", "simonw"], "剥掉 @ 与完整 URL")

        settings.handlesText = "a b|c;d, ../../etc/passwd"
        expect(!settings.handles.contains { $0.contains("/") || $0.contains(";") },
               "非法字符与路径穿越被剥掉")

        settings.handlesText = "   ,  , "
        expect(settings.handles.isEmpty, "全空白视为「用 config.toml 全部账号」")

        settings = MonitorSettings()
        expect(settings.isValid, "默认设置合法")
        expectEqual(settings.intervalMinutes, 20, "默认间隔 20 分钟")
        settings.intervalMinutes = 0
        expect(!settings.isValid, "间隔 0 非法")
        settings.intervalMinutes = 5
        settings.threshold = 1.5
        expect(!settings.isValid, "阈值 >1 非法")
        settings.threshold = 0.7
        settings.projectPath = "   "
        expect(!settings.isValid, "空项目路径非法")

        let original = MonitorSettings(
            projectPath: "/tmp/x", handlesText: "a,b", intervalMinutes: 7,
            threshold: 0.42, judgeEnabled: true, isEnabled: true
        )
        if let data = try? JSONEncoder().encode(original),
           let restored = try? JSONDecoder().decode(MonitorSettings.self, from: data) {
            expectEqual(restored, original, "设置 JSON 往返")
        } else {
            expect(false, "设置 JSON 往返（编解码抛错）")
        }
    }

    private static func urlSafety() {
        print("· 链接安全（帖子里的链接是不可信输入）")
        func verdict(_ url: String) -> ResetVerdict {
            ResetVerdict(
                postID: "1", authorHandle: "thsottiaux", text: "Reset all propagated.",
                url: url, publishedAt: nil, probability: 0.95,
                scope: "usage_limits", reasoning: "ok", model: "claude-opus-5"
            )
        }
        expect(verdict("https://x.com/a/status/1").safeURL != nil, "接受 https")
        expect(verdict("http://x.com/a/status/1").safeURL == nil, "拒绝 http")
        expect(verdict("javascript:alert(1)").safeURL == nil, "拒绝 javascript:")
        expect(verdict("file:///etc/passwd").safeURL == nil, "拒绝 file:")
        expect(verdict("").safeURL == nil, "拒绝空链接")
    }

    private static func coverageHonesty() {
        print("· 覆盖结论不得比事实更确定")
        func state(_ coverage: String?) -> AccountState? {
            let value = coverage.map { "\"\($0)\"" } ?? "null"
            let json = """
            {"handle":"a","last_attempt_at":null,"last_valid_response_at":null,
             "coverage_status":\(value),"consecutive_failures":0,"gap_from_at":null}
            """
            return try? JSONDecoder().decode(AccountState.self, from: Data(json.utf8))
        }
        expect(state("overlap_observed")?.coverageIsProven == true, "overlap_observed 算已证明")
        expect(state("source_exhausted")?.coverageIsProven == true, "source_exhausted 算已证明")
        expect(state("bootstrap")?.coverageIsProven == true, "bootstrap 算已证明")
        expect(state("limited")?.coverageIsProven == false, "limited 不算已证明")
        expect(state("unknown")?.coverageIsProven == false, "unknown 不算已证明")
        expect(state(nil)?.coverageIsProven == false, "无结论不算已证明")
    }

    private static func languageSwitching() {
        print("· 中英切换（默认中文，无译文回落原文）")

        expectEqual(DisplayLanguage.chinese.toggled, .original, "中 → EN")
        expectEqual(DisplayLanguage.original.toggled, .chinese, "EN → 中")
        expectEqual(DisplayLanguage.chinese.label, "中", "中文按钮文案")
        expectEqual(DisplayLanguage.original.label, "EN", "英文按钮文案")
        expectEqual(MonitorSettings().displayLanguage, .chinese, "默认显示中文")

        // 有译文：中文模式给译文，原文模式给原文
        expectEqual(
            DisplayLanguage.chinese.pick(original: "Reset all propagated.",
                                         translated: "重置已全部生效。"),
            "重置已全部生效。", "中文模式取译文"
        )
        expectEqual(
            DisplayLanguage.original.pick(original: "Reset all propagated.",
                                          translated: "重置已全部生效。"),
            "Reset all propagated.", "原文模式取原文"
        )
        // 没译文：回落原文，**不能**显示空白
        expectEqual(
            DisplayLanguage.chinese.pick(original: "Welcome", translated: nil),
            "Welcome", "无译文回落原文"
        )
        expectEqual(
            DisplayLanguage.chinese.pick(original: "Welcome", translated: ""),
            "Welcome", "空译文也回落原文"
        )

        func post(_ zh: String?) -> RecentPost? {
            let value = zh.map { "\"\($0)\"" } ?? "null"
            let json = """
            {"post_id":"1","monitored_handle":"thsottiaux","text":"Welcome",
             "text_zh":\(value),"url":"https://x.com/a/status/1",
             "published_at":null,"post_type":"reply","is_reset":null,"probability":null}
            """
            return try? JSONDecoder().decode(RecentPost.self, from: Data(json.utf8))
        }
        expectEqual(post("欢迎")?.displayText(.chinese), "欢迎", "帖子按语言取正文")
        expectEqual(post("欢迎")?.displayText(.original), "Welcome", "帖子取原文")
        expect(post("欢迎")?.isFallingBack(to: .chinese) == false, "有译文不算回落")
        // 想看中文却没译文 → 必须能被界面识别出来并标注「原文」
        expect(post(nil)?.isFallingBack(to: .chinese) == true, "无译文时标记为回落")
        expect(post(nil)?.isFallingBack(to: .original) == false, "原文模式不算回落")
    }

    private static func jsonLineExtraction() {
        print("· 从 stdout 取 JSON（日志走 stderr）")
        let multi = "{\"schema\":1,\"stale\":true}\n{\"schema\":1,\"fresh\":true}"
        expect(XWatchRunner.lastJSONLine(in: multi)?.contains("fresh") == true, "取最后一个对象")

        let indented = "{\n  \"schema\": 1,\n  \"resets\": []\n}"
        expect(XWatchRunner.lastJSONLine(in: indented) != nil, "支持缩进的整段对象")

        expect(XWatchRunner.lastJSONLine(in: "采集完成\n退出码 0\n") == nil, "纯日志不误判")
        expect(XWatchRunner.lastJSONLine(in: "") == nil, "空输出不误判")
        expect(XWatchRunner.lastJSONLine(in: "{not json}") == nil, "伪 JSON 不误判")
    }

    private static func reportDecoding() {
        print("· 契约解码（字段名对齐 report.py）")
        // 下面这段是从真实 run-once --json 输出裁剪来的
        let sample = """
        {
          "schema": 1,
          "generated_at": "2026-09-17T11:05:00+00:00",
          "ok": true,
          "exit_code": 0,
          "judge": {"enabled": true, "backend": "claude_cli", "model": "claude-opus-5",
                    "threshold": 0.7, "calls_last_24h": 6, "max_calls_per_day": 200},
          "accounts": [{"handle": "thsottiaux", "last_attempt_at": "2026-09-17T11:04:00+00:00",
                        "last_valid_response_at": "2026-09-17T11:04:01+00:00",
                        "coverage_status": "overlap_observed", "consecutive_failures": 0,
                        "gap_from_at": null}],
          "resets": [{"post_id": "2098685367058612394", "author_handle": "thsottiaux",
                      "text": "Reset all propagated. Sweet dreams.",
                      "url": "https://x.com/thsottiaux/status/2098685367058612394",
                      "published_at": "2026-09-12T08:09:00+00:00", "probability": 0.95,
                      "scope": "usage_limits", "reasoning": "announced",
                      "model": "claude-opus-5", "judged_at": "2026-09-17T11:00:00+00:00",
                      "notified_at": null}],
          "collection": {"accounts": 1, "new_posts": 2, "pages_fetched": 1,
                         "worst_status": "success", "aborted_reason": null},
          "judge_run": {"considered": 2, "judged": 2, "failed": 0, "triggered": 1, "notes": []},
          "errors": []
        }
        """
        guard let report = decode(sample) else {
            expect(false, "解码真实形状的报告")
            return
        }
        expectEqual(report.schema, 1, "schema")
        expectEqual(report.ok, true, "ok")
        expectEqual(report.accounts.count, 1, "accounts 条数")
        expectEqual(report.resets.count, 1, "resets 条数")
        expect(abs(report.resets[0].probability - 0.95) < 0.0001, "probability 解码")
        expectEqual(report.resets[0].scope, "usage_limits", "scope 解码")
        expect(report.resets[0].notifiedAt == nil, "notified_at 为 null")
        expectEqual(report.collection?.newPosts, 2, "collection.new_posts")
        expectEqual(report.judgeRun?.triggered, 1, "judge_run.triggered")
        expectEqual(report.judge?.backend, "claude_cli", "judge.backend")

        // x-watch 的 report.REPORT_SCHEMA 提升时这里必须同步改
        expectEqual(TickReport.supportedSchema, 3, "支持的契约版本与 Python 端一致")

        if let minimal = decode("{\"schema\": 1, \"accounts\": [], \"resets\": []}") {
            expect(minimal.collection == nil, "缺省可选段落仍能解码")
        } else {
            expect(false, "最小报告解码")
        }
    }

    private static func forecastAndPosts() {
        print("· 今日概率与最近帖子")

        // schema 1 的老报告不能因为缺新字段就解码失败
        if let legacy = decode("{\"schema\": 1, \"accounts\": [], \"resets\": []}") {
            expect(legacy.recentPosts == nil, "缺 recent_posts 时为 nil 而不是报错")
            expect(legacy.forecasts == nil, "缺 forecasts 时为 nil 而不是报错")
        } else {
            expect(false, "schema 1 老报告仍能解码")
        }

        let json = """
        {
          "schema": 2, "accounts": [], "resets": [],
          "recent_posts": [
            {"post_id": "1", "monitored_handle": "thsottiaux",
             "text": "Reset all propagated.", "text_zh": "重置已全部生效。好梦。",
             "url": "https://x.com/thsottiaux/status/1",
             "published_at": "2026-09-12T08:09:00+00:00",
             "post_type": "original", "is_reset": true, "probability": 0.95},
            {"post_id": "2", "monitored_handle": "thsottiaux", "text": "hi",
             "url": "http://insecure.example/2",
             "published_at": "2026-09-17T01:00:00+00:00",
             "post_type": "reply", "is_reset": null, "probability": null}
          ],
          "forecasts": [
            {"date_key": "2026-09-17", "handle": "thsottiaux", "probability": 0.04,
             "confidence": "medium", "signals": ["96% of day elapsed", "last reset 5 days ago"],
             "reasoning": "quiet day", "model": "claude-opus-5",
             "created_at": "2026-09-17T15:06:26+00:00",
             "already_reset_today": false, "cached": true}
          ]
        }
        """
        guard let report = decode(json) else {
            expect(false, "解码 schema 2 报告")
            return
        }
        expectEqual(report.recentPosts?.count, 2, "recent_posts 条数")
        let posts = report.recentPosts ?? []
        expectEqual(posts.first?.typeLabel, "原创", "post_type 中文标签")
        expectEqual(posts.first?.textZh, "重置已全部生效。好梦。", "text_zh 解码")
        expectEqual(posts.last?.textZh, nil, "缺 text_zh 时为 nil")
        expectEqual(posts.last?.typeLabel, "回复", "reply 标签")
        expect(posts.first?.isReset == true, "已判定的帖子带 is_reset")
        expect(posts.last?.isReset == nil, "未判定的帖子 is_reset 为 nil")
        expect(posts.last?.probability == nil, "未判定的帖子无概率")
        // 帖子链接同样只接受 https
        expect(posts.first?.safeURL != nil, "https 帖子链接可用")
        expect(posts.last?.safeURL == nil, "http 帖子链接被拒")

        let forecast = report.forecasts?.first
        expectEqual(forecast?.percentText, "4%", "概率百分比文案")
        expectEqual(forecast?.confidenceLabel, "置信度中", "置信度标签")
        expectEqual(forecast?.signals.count, 2, "signals 条数")
        expect(forecast?.hasResetToday == false, "今天未重置")
        expect(forecast?.cached == true, "缓存标记")

        // 今天已重置时要走「已重置」分支，不显示预测数字
        let resetToday = """
        {"schema": 2, "accounts": [], "resets": [],
         "forecasts": [{"date_key": "2026-09-12", "handle": "thsottiaux",
           "probability": 1.0, "confidence": "high", "signals": [],
           "reasoning": "already", "model": "-", "created_at": null,
           "already_reset_today": true, "cached": false}]}
        """
        if let report2 = decode(resetToday) {
            expect(report2.forecasts?.first?.hasResetToday == true, "already_reset_today 生效")
        } else {
            expect(false, "解码已重置的预测")
        }
    }

    private static func runnerConfiguration() {
        print("· 运行器配置（GUI 不继承 shell PATH）")
        let runner = XWatchRunner(projectPath: "/tmp/x-watch")
        expectEqual(runner.configPath, "/tmp/x-watch/config.toml", "config.toml 路径")

        let missing = XWatchRunner(projectPath: "/definitely/not/here")
        var threw = false
        do { try missing.validate() } catch { threw = true }
        expect(threw, "缺失项目目录要报错")

        expect(XWatchRunner.extraPathEntries.contains("/opt/homebrew/bin"),
               "补 PATH 含 Homebrew（否则 Finder 启动时找不到 claude）")
        expect(XWatchRunner.extraPathEntries.contains("/usr/local/bin"), "补 PATH 含 /usr/local/bin")
        expectEqual(XWatchRunner.systemPython, "/usr/bin/python3", "固定用系统 python3")
    }

    private static func decode(_ json: String) -> TickReport? {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime]
            if let date = formatter.date(from: raw) { return date }
            throw DecodingError.dataCorruptedError(in: container, debugDescription: raw)
        }
        return try? decoder.decode(TickReport.self, from: Data(json.utf8))
    }
}
