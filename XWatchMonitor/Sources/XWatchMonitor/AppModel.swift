import AppKit
import Foundation
import XWatchCore

@MainActor
final class AppModel: ObservableObject {
    @Published var settings: MonitorSettings
    @Published private(set) var status = "请先确认项目路径，然后点「保存并开始监控」"
    @Published private(set) var isChecking = false
    @Published private(set) var latestResets: [ResetVerdict] = []
    @Published private(set) var accounts: [AccountState] = []
    @Published private(set) var recentPosts: [RecentPost] = []
    @Published private(set) var forecasts: [ResetForecast] = []
    @Published private(set) var lastCheckedAt: Date?
    @Published private(set) var lastError: String?
    @Published private(set) var judgeInfo: JudgeInfo?
    @Published private(set) var loginItemEnabled = LoginItemService.isEnabled
    @Published private(set) var loginItemStatus = LoginItemService.statusDescription
    let loginItemWarning = LoginItemService.locationWarning

    private let defaults = UserDefaults.standard
    private var monitorTask: Task<Void, Never>?
    private let settingsKey = "xwatch.monitorSettings.v1"
    private let seenIDsKey = "xwatch.seenResetIDs.v1"
    private let baselineKey = "xwatch.hasBaseline.v1"

    init() {
        if let data = defaults.data(forKey: settingsKey),
           let stored = try? JSONDecoder().decode(MonitorSettings.self, from: data) {
            settings = stored
        } else {
            // 首次启动：默认就开始监控。
            //
            // 这是个「常驻监控」app —— 装好却默认不监控是反直觉的，而且很容易
            // 误以为它在工作（实测踩过：app 在运行、点过一次「立即检查」，
            // 但定时器从未启动，因为设置没保存过）。
            // 默认值是完整可用的（项目路径 + thsottiaux + 20 分钟），所以直接开。
            var initial = MonitorSettings()
            initial.isEnabled = XWatchRunner(projectPath: initial.projectPath).isReady
            settings = initial
            if initial.isEnabled {
                persistSettings()
            } else {
                status = "找不到 x-watch 项目，请到设置里指定目录后点「保存并开始监控」"
            }
        }

        if settings.isEnabled, settings.isValid {
            scheduleStartAfterLaunch()
        } else if !settings.isEnabled {
            status = "未在监控 —— 到设置里点「保存并开始监控」，或用下面的「开始监控」按钮"
        }
    }

    /// 界面要能一眼看出到底有没有在按周期跑。
    var isMonitoring: Bool { settings.isEnabled && monitorTask != nil }

    deinit {
        monitorTask?.cancel()
    }

    // MARK: - 生命周期

    func saveAndStart() async {
        guard settings.isValid else {
            status = "设置不合法：检查项目路径、间隔（1–1440 分钟）和阈值（0–1）"
            return
        }

        let runner = XWatchRunner(projectPath: settings.projectPath)
        do {
            try runner.validate()
        } catch {
            status = error.localizedDescription
            return
        }

        do {
            let granted = try await NotificationService.shared.requestPermission()
            guard granted else {
                status = "通知权限未开启，已打开系统设置；请允许「X 重置监控」通知后，再点一次「保存并开始监控」"
                return
            }
        } catch {
            status = "申请通知权限失败：\(error.localizedDescription)"
            return
        }

        settings.isEnabled = true
        persistSettings()
        // 首次启动把已有命中当基线，避免历史公告一次性刷屏
        await checkNow(markCurrentAsBaseline: !defaults.bool(forKey: baselineKey))
        startTimer()
    }

    /// 菜单里的一键开始（不必进设置面板）。
    func startFromMenu() async {
        await saveAndStart()
    }

    func stop() {
        monitorTask?.cancel()
        monitorTask = nil
        settings.isEnabled = false
        persistSettings()
        status = "监控已暂停"
    }

    /// 只列候选、不调模型 —— 用来在开判定之前估算量。
    func judgeDryRun() async {
        guard !isChecking else { return }
        isChecking = true
        defer { isChecking = false }
        status = "正在列出候选帖子（不调模型）…"
        let runner = XWatchRunner(projectPath: settings.projectPath)
        do {
            let output = try await runner.judgeDryRun(handles: settings.handles)
            let lines = output.components(separatedBy: .newlines).filter { $0.contains("候选") }
            status = lines.last ?? "空跑完成，没有候选帖子"
        } catch {
            status = "空跑失败：\(error.localizedDescription)"
        }
    }

    // MARK: - 显示语言

    /// 一键在中文译文和英文原文之间切换。立刻持久化，重启后保持。
    func toggleDisplayLanguage() {
        settings.displayLanguage = settings.displayLanguage.toggled
        persistSettings()
    }

    var displayLanguage: DisplayLanguage { settings.displayLanguage }

    /// 当前显示的条目里，有多少条还没有中文译文。
    var missingTranslationCount: Int {
        guard settings.displayLanguage == .chinese else { return 0 }
        let posts = recentPosts.prefix(6).filter { $0.isFallingBack(to: .chinese) }.count
        let hits = latestResets.filter { $0.isFallingBack(to: .chinese) }.count
        return posts + hits
    }

    // MARK: - 开机启动

    func setLoginItem(_ enabled: Bool) {
        if let error = LoginItemService.setEnabled(enabled) {
            status = error
        }
        // 以系统的真实状态为准，不要以点击意图为准（可能停在 requiresApproval）
        loginItemEnabled = LoginItemService.isEnabled
        loginItemStatus = LoginItemService.statusDescription
    }

    // MARK: - 一轮检查

    func checkNow(markCurrentAsBaseline: Bool = false) async {
        guard !isChecking else { return }
        guard settings.isValid else {
            status = "设置不合法，无法检查"
            return
        }

        isChecking = true
        status = "正在采集…"
        defer { isChecking = false }

        let runner = XWatchRunner(projectPath: settings.projectPath)
        let report: TickReport
        do {
            report = try await runner.runTick(handles: settings.handles, judge: settings.judgeEnabled)
        } catch is CancellationError {
            return
        } catch {
            lastError = error.localizedDescription
            status = "检查失败：\(error.localizedDescription)"
            return
        }

        // 本轮被跳过（另一个进程持有锁）：保留上一轮的数据，不清空、不报错
        if report.wasSkipped {
            status = "本轮跳过：" + (report.skippedReason ?? "另一个进程正在写数据库")
            return
        }

        lastCheckedAt = Date()
        // 只显示本次设置中实际监控的账号。空白表示 config.toml 全部启用。
        let selectedHandles = Set(settings.handles)
        accounts = selectedHandles.isEmpty
            ? report.accounts
            : report.accounts.filter { selectedHandles.contains($0.handle.lowercased()) }
        judgeInfo = report.judge
        // 只展示被监控账号自己的帖子，新→旧
        recentPosts = (report.recentPosts ?? [])
            .sorted { ($0.publishedAt ?? .distantPast) > ($1.publishedAt ?? .distantPast) }
        forecasts = report.forecasts ?? []
        lastError = report.errors?.first

        // 只保留达到阈值的命中；概率是模型判断，不是事实
        // 同一帖子可能因历史判定版本或旧数据出现多行；UI 按 post_id 只展示一次。
        var seenPostIDs = Set<String>()
        let hits = report.resets
            .filter { $0.probability >= settings.threshold }
            .filter { verdict in
                guard seenPostIDs.insert(verdict.postID).inserted else { return false }
                return true
            }
            .sorted { ($0.publishedAt ?? .distantPast) > ($1.publishedAt ?? .distantPast) }
        latestResets = hits

        var seen = Set(defaults.stringArray(forKey: seenIDsKey) ?? [])
        let hasBaseline = defaults.bool(forKey: baselineKey)

        if markCurrentAsBaseline || !hasBaseline {
            seen.formUnion(hits.map(\.postID))
            defaults.set(Array(seen), forKey: seenIDsKey)
            defaults.set(true, forKey: baselineKey)
            status = baselineStatus(report: report, hits: hits)
            return
        }

        let fresh = hits.filter { !seen.contains($0.postID) }
        var delivered = 0
        for verdict in fresh.prefix(5) {
            do {
                try await NotificationService.shared.deliverReset(verdict)
                delivered += 1
            } catch {
                lastError = "发送通知失败：\(error.localizedDescription)"
            }
        }
        seen.formUnion(hits.map(\.postID))
        defaults.set(Array(seen), forKey: seenIDsKey)

        status = tickStatus(report: report, newHits: fresh.count, delivered: delivered)
    }

    // MARK: - 状态文案

    private func baselineStatus(report: TickReport, hits: [ResetVerdict]) -> String {
        var parts = ["监控已启动：当前 \(hits.count) 条历史命中已设为基线，之后只通知新增"]
        if let judge = report.judge, !judge.isActive {
            parts.append("注意：判定未开启，不会产生新命中 —— 到设置里打开「调大模型判定」")
        }
        return parts.joined(separator: "。")
    }

    /// 要在弹窗顶部显示的预测。优先显示当前关注的账号。
    var primaryForecast: ResetForecast? {
        let wanted = settings.handles
        if let first = wanted.first,
           let match = forecasts.first(where: { $0.handle == first }) {
            return match
        }
        return forecasts.first
    }

    /// 菜单栏标题中的当前重置概率；今天已重置时显示状态而非误导性的概率。
    var menuBarProbabilityText: String {
        guard let forecast = primaryForecast else { return "—" }
        return forecast.hasResetToday ? "已重置" : forecast.percentText
    }

    private func tickStatus(report: TickReport, newHits: Int, delivered: Int) -> String {
        var parts: [String] = []
        if let collection = report.collection {
            parts.append("采集 \(collection.newPosts ?? 0) 条新帖")
        }
        if let run = report.judgeRun, let judged = run.judged {
            parts.append("判定 \(judged) 条")
            if let failed = run.failed, failed > 0 {
                parts.append("判定失败 \(failed) 条")
            }
        }
        if newHits > 0 {
            parts.append("命中 \(newHits) 条，已通知 \(delivered) 条")
        } else {
            parts.append("无新命中")
        }
        // 覆盖结论没被证明时要说出来，不能让界面显得比事实更确定
        let unproven = report.accounts.filter { !$0.coverageIsProven }.map(\.handle)
        if !unproven.isEmpty {
            parts.append("未证明覆盖完整：\(unproven.joined(separator: "、"))")
        }
        if let error = report.errors?.first {
            parts.append("错误：\(error)")
        }
        return parts.joined(separator: "；")
    }

    // MARK: - 定时器

    private func scheduleStartAfterLaunch() {
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard let self else { return }
            await self.checkNow(
                markCurrentAsBaseline: !self.defaults.bool(forKey: self.baselineKey)
            )
            self.startTimer()
        }
    }

    private func startTimer() {
        monitorTask?.cancel()
        let interval = max(1, settings.intervalMinutes)
        monitorTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: UInt64(interval) * 60 * 1_000_000_000)
                } catch {
                    return
                }
                guard let self, !Task.isCancelled else { return }
                await self.checkNow()
            }
        }
    }

    private func persistSettings() {
        if let data = try? JSONEncoder().encode(settings) {
            defaults.set(data, forKey: settingsKey)
        }
    }

    /// 清掉去重记录，下一轮会把当前命中重新当成新的。排查用。
    func resetBaseline() {
        defaults.removeObject(forKey: seenIDsKey)
        defaults.removeObject(forKey: baselineKey)
        status = "已清除去重记录，下次检查会重新设基线"
    }
}
