import AppKit
import SwiftUI
import XWatchCore

struct ContentView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("X 重置监控")
                    .font(.title2.weight(.semibold))
                Text("定期跑 x-watch 采集指定博主的公开帖子，用大模型判断是否在宣布用量限额重置，命中就弹通知。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                projectSection
                Divider()
                monitorSection
                Divider()
                loginItemSection
                Divider()
                buttons

                if !model.status.isEmpty {
                    Divider()
                    Text(model.status)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = model.lastError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Divider()
                caveats
            }
            .padding(22)
            .frame(width: 560, alignment: .leading)
        }
        .frame(width: 560, height: 640)
    }

    private var projectSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("x-watch 项目")
                .font(.headline)
            HStack {
                TextField("含 config.toml 的目录", text: $model.settings.projectPath)
                    .textFieldStyle(.roundedBorder)
                Button("选择…") { pickProject() }
            }
            Text("采集策略（账号清单、页数预算、判定后端与阈值）都在该目录的 config.toml 里改。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var monitorSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("监控")
                .font(.headline)

            VStack(alignment: .leading, spacing: 4) {
                Text("账号（逗号分隔，留空＝config.toml 里全部启用账号）")
                    .font(.caption)
                TextField("thsottiaux", text: $model.settings.handlesText)
                    .textFieldStyle(.roundedBorder)
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("检查间隔")
                        .font(.caption)
                    Spacer()
                    Text("\(model.settings.intervalMinutes) 分钟")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Slider(
                    value: Binding(
                        get: { Double(model.settings.intervalMinutes) },
                        set: { model.settings.intervalMinutes = Int($0) }
                    ),
                    in: 1...120,
                    step: 1
                )
                Text("端到端延迟 ≈ 这个间隔（上游数据是实时的）。5 分钟约占上游配额的 0.06%。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Toggle(isOn: $model.settings.judgeEnabled) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("调大模型判定")
                    Text("关掉就只采集归档、不判断是否为重置公告，也就不会有通知。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("通知阈值")
                        .font(.caption)
                    Spacer()
                    Text(String(format: "%.2f", model.settings.threshold))
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Slider(value: $model.settings.threshold, in: 0.1...1.0, step: 0.05)
                Text("模型判定为「是」且概率达到该值才通知。概率是模型的判断，不是事实。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var loginItemSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("开机启动")
                .font(.headline)
            Toggle(isOn: Binding(
                get: { model.loginItemEnabled },
                set: { model.setLoginItem($0) }
            )) {
                Text("登录后自动启动本 app")
            }
            Text(model.loginItemStatus)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let warning = model.loginItemWarning {
                Text(warning)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var buttons: some View {
        HStack(spacing: 10) {
            Button {
                Task { await model.saveAndStart() }
            } label: {
                Label("保存并开始监控", systemImage: "play.fill")
            }
            .keyboardShortcut(.defaultAction)
            .disabled(model.isChecking)

            Button {
                Task { await model.judgeDryRun() }
            } label: {
                Label("空跑（不调模型）", systemImage: "eye")
            }
            .disabled(model.isChecking)

            Spacer()

            Button("清除去重记录") { model.resetBaseline() }
                .help("下次检查会把当前命中重新当成新的。排查用。")
        }
    }

    private var caveats: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("已知限制")
                .font(.headline)
            caveat("Mac 休眠、关机或断网期间不会检查；唤醒后按间隔继续，不补跑错过的次数。")
            caveat("退出 app 就停止检查。开机启动只保证登录后自动拉起，不会在退出后自动重开。")
            caveat("判定用的模型、后端和预算在 config.toml 的 [judge] 段里改；这里的开关只决定本 app 每轮是否判定。")
            caveat("覆盖结论显示 limited / unknown 时，表示本轮没能证明那段时间没有遗漏。")
        }
    }

    private func caveat(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Text("•")
            Text(text).fixedSize(horizontal: false, vertical: true)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private func pickProject() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: model.settings.projectPath)
        if panel.runModal() == .OK, let url = panel.url {
            model.settings.projectPath = url.path
        }
    }
}
