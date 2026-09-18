import AppKit
import SwiftUI
import XWatchCore

@main
struct XWatchMonitorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        Settings {
            ContentView(model: model)
        }

        MenuBarExtra {
            MenuBarContent(model: model)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: model.isChecking
                      ? "arrow.triangle.2.circlepath"
                      : "arrow.counterclockwise.circle")
                Text(model.menuBarProbabilityText)
                    .font(.caption.monospacedDigit())
                    .help("今日重置概率；已重置时显示已重置")
            }
        }
        .menuBarExtraStyle(.window)
    }
}

private struct MenuBarContent: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if !model.settings.isEnabled {
                notMonitoringBanner
            }
            Divider()
            forecastRow
            Divider()
            statusRow

            if !model.latestResets.isEmpty {
                Divider()
                resetList
            }

            if !model.recentPosts.isEmpty {
                Divider()
                recentPostList
            }

            if !model.accounts.isEmpty {
                Divider()
                coverageRow
            }

            Divider()
            actions
        }
        .padding(14)
        .frame(width: 420)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "bell.badge.fill")
                .font(.title2)
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text("X 重置监控")
                    .font(.headline)
                Text(model.settings.handles.isEmpty
                     ? "未指定账号（用 config.toml 里全部启用账号）"
                     : model.settings.handles.map { "@" + $0 }.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            // 中英切换。默认中文，点一下看原文。
            Button {
                model.toggleDisplayLanguage()
            } label: {
                Text(model.displayLanguage.label)
                    .font(.caption.weight(.semibold))
                    .frame(width: 26)
            }
            .buttonStyle(.bordered)
            .help(model.displayLanguage.help)

            Circle()
                .fill(model.settings.isEnabled ? Color.green : Color.gray)
                .frame(width: 8, height: 8)
        }
    }

    /// 没在监控时必须显眼 —— 否则很容易误以为它在工作。
    private var notMonitoringBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "pause.circle.fill")
                .foregroundStyle(.orange)
            Text("未在按周期检查")
                .font(.callout.weight(.medium))
            Spacer()
            Button("开始监控") {
                Task { await model.startFromMenu() }
            }
            .controlSize(.small)
            .disabled(model.isChecking)
        }
        .padding(8)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
    }

    /// 今日重置概率。今天已经重置过就直接说已重置，不显示预测数字。
    @ViewBuilder
    private var forecastRow: some View {
        if let forecast = model.primaryForecast {
            if forecast.hasResetToday {
                HStack(spacing: 10) {
                    Image(systemName: "checkmark.seal.fill")
                        .font(.title)
                        .foregroundStyle(.green)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("今天已经重置过了")
                            .font(.headline)
                        Text("@\(forecast.handle) · \(forecast.dateKey)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(forecast.percentText)
                            .font(.system(size: 30, weight: .semibold, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(Self.tint(for: forecast.probability))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("今天还会重置的概率")
                                .font(.callout.weight(.medium))
                            Text("\(forecast.confidenceLabel) · \(forecast.dateKey)"
                                 + (forecast.cached == true ? " · 缓存" : ""))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    if !forecast.signals.isEmpty {
                        // 模型给的判断依据。这是不可信输出，只当文本展示
                        FlowSignals(signals: forecast.signals)
                    }
                    if !forecast.reasoning.isEmpty {
                        Text(forecast.reasoning)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text("这是模型的估计，不是事实。")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        } else {
            HStack(spacing: 8) {
                Image(systemName: "questionmark.circle")
                    .foregroundStyle(.secondary)
                Text(model.settings.judgeEnabled
                     ? "还没有今天的概率 —— 下一轮检查会算"
                     : "判定已关闭，不会计算今日概率")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// 按当前显示语言取正文；空正文给个占位。
    private func displayed(_ post: RecentPost) -> String {
        let text = post.displayText(model.displayLanguage)
        return text.isEmpty ? "（无文字内容）" : text
    }

    private func displayed(_ verdict: ResetVerdict) -> String {
        let text = verdict.displayText(model.displayLanguage)
        return text.isEmpty ? "（无文字内容）" : text
    }

    private static func tint(for probability: Double) -> Color {
        if probability >= 0.6 { return .orange }
        if probability >= 0.25 { return .yellow }
        return .secondary
    }

    /// 最近帖子（不只是命中）。判定过的会显示概率。
    private var recentPostList: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("最近帖子")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if model.missingTranslationCount > 0 {
                    Text("\(model.missingTranslationCount) 条待译")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            ForEach(Array(model.recentPosts.prefix(6))) { post in
                Button {
                    if let url = post.safeURL { NSWorkspace.shared.open(url) }
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(displayed(post))
                                .font(.callout)
                                .lineLimit(2)
                                .multilineTextAlignment(.leading)
                            HStack(spacing: 6) {
                                Text("@\(post.monitoredHandle)")
                                Text(post.typeLabel)
                                if post.isFallingBack(to: model.displayLanguage) {
                                    Text("原文")
                                        .foregroundStyle(.tertiary)
                                }
                                Text(post.publishedAt?.formatted(date: .abbreviated,
                                                                 time: .shortened) ?? "时间未知")
                                if let probability = post.probability {
                                    Text(String(format: "判定 %.0f%%", probability * 100))
                                        .foregroundStyle(
                                            (post.isReset ?? false) ? Color.orange : Color.secondary
                                        )
                                } else {
                                    Text("未判定")
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Image(systemName: "arrow.up.right")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(post.safeURL == nil)
            }
        }
    }

    private var statusRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 8) {
                if model.isChecking {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                }
                Text(model.status)
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                if let at = model.lastCheckedAt {
                    Text("上次检查 " + at.formatted(date: .omitted, time: .standard))
                }
                Text(model.settings.isEnabled
                     ? "每 \(model.settings.intervalMinutes) 分钟"
                     : "已暂停")
                if let judge = model.judgeInfo {
                    Text(judge.isActive ? "判定开启" : "判定关闭")
                        .foregroundStyle(judge.isActive ? Color.secondary : Color.orange)
                    if let calls = judge.callsLast24h, let cap = judge.maxCallsPerDay {
                        Text("24h 调用 \(calls)/\(cap)")
                    }
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private var resetList: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("命中（概率 ≥ \(String(format: "%.2f", model.settings.threshold))）")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            ForEach(Array(model.latestResets.prefix(4))) { verdict in
                Button {
                    if let url = verdict.safeURL { NSWorkspace.shared.open(url) }
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Text(String(format: "%.0f%%", verdict.probability * 100))
                            .font(.caption.weight(.bold).monospacedDigit())
                            .foregroundStyle(verdict.probability >= 0.9 ? .orange : .secondary)
                            .frame(width: 38, alignment: .trailing)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(displayed(verdict))
                                .font(.callout)
                                .lineLimit(2)
                            HStack(spacing: 5) {
                                Text("@\(verdict.authorHandle)")
                                Text(verdict.publishedAt?.formatted(date: .abbreviated,
                                                                    time: .shortened) ?? "时间未知")
                                if verdict.isFallingBack(to: model.displayLanguage) {
                                    Text("原文")
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Image(systemName: "arrow.up.right")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(verdict.safeURL == nil)
            }
        }
    }

    private var coverageRow: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(model.accounts) { account in
                HStack(spacing: 6) {
                    Image(systemName: account.coverageIsProven
                          ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                        .font(.caption2)
                        .foregroundStyle(account.coverageIsProven ? .green : .orange)
                    Text("@\(account.handle)")
                        .font(.caption)
                    Text(account.coverageStatus ?? "无结论")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if account.consecutiveFailures > 0 {
                        Text("连续失败 \(account.consecutiveFailures)")
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                    if account.gapFromAt != nil {
                        Text("有缺口")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
            }
        }
    }

    private var actions: some View {
        HStack {
            Button {
                Task { await model.checkNow() }
            } label: {
                Label("立即检查", systemImage: "arrow.clockwise")
            }
            .disabled(model.isChecking)

            if model.settings.isEnabled {
                Button("暂停") { model.stop() }
                    .disabled(model.isChecking)
            }

            Spacer()

            if #available(macOS 14.0, *) {
                SettingsLink { Text("设置…") }
            } else {
                Button("设置…") {
                    _ = NSApp.sendAction(
                        Selector(("showPreferencesWindow:")), to: nil, from: nil
                    )
                    NSApp.activate(ignoringOtherApps: true)
                }
            }

            Button {
                NSApp.terminate(nil)
            } label: {
                Image(systemName: "power")
            }
            .help("退出")
        }
    }
}

/// 把模型给出的若干「信号」短语排成可换行的小标签。
/// 内容来自模型输出（不可信），这里只做展示，不解释成任何标记。
private struct FlowSignals: View {
    let signals: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(signals.enumerated()), id: \.offset) { _, signal in
                HStack(alignment: .top, spacing: 5) {
                    Image(systemName: "circle.fill")
                        .font(.system(size: 4))
                        .foregroundStyle(.secondary)
                        .padding(.top, 5)
                    Text(signal)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}
