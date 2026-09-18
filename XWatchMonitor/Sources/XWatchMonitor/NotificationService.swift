import AppKit
import Foundation
import UserNotifications
import XWatchCore

final class NotificationService {
    static let shared = NotificationService()

    private init() {}

    enum PermissionError: LocalizedError {
        case denied

        var errorDescription: String? {
            "通知权限已被系统拒绝"
        }
    }

    func requestPermission() async throws -> Bool {
        let center = UNUserNotificationCenter.current()
        let current = await center.notificationSettings()
        if current.authorizationStatus == .denied {
            openNotificationSettings()
            return false
        }

        do {
            let granted = try await center.requestAuthorization(options: [.alert, .sound])
            if !granted {
                openNotificationSettings()
            }
            return granted
        } catch {
            // macOS 在已拒绝状态下可能直接抛出，而不是返回 false。
            let after = await center.notificationSettings()
            if after.authorizationStatus == .denied {
                openNotificationSettings()
                return false
            }
            throw error
        }
    }

    /// 打开当前 App 对应的系统通知设置；用户开启后需再次点击开始监控。
    private func openNotificationSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings") {
            NSWorkspace.shared.open(url)
        }
    }

    /// 弹一条重置命中通知。
    ///
    /// **通知内容全是不可信文本**（帖子正文来自公开时间线，reasoning 来自模型输出），
    /// 所以这里压成单行、剥控制字符、限长；URL 只接受 https。
    func deliverReset(_ verdict: ResetVerdict) async throws {
        let content = UNMutableNotificationContent()
        content.title = String(format: "疑似 reset（%.0f%%）", verdict.probability * 100)
        content.subtitle = "@" + Self.sanitize(verdict.authorHandle, limit: 20)
        content.body = Self.sanitize(verdict.text, limit: 200)
        content.sound = .default
        if let url = verdict.safeURL {
            content.userInfo = ["url": url.absoluteString]
        }
        let request = UNNotificationRequest(
            // 同一条帖子只会有一个通知 ID，系统层面再兜一层去重
            identifier: "reset-\(verdict.postID)",
            content: content,
            trigger: nil
        )
        try await UNUserNotificationCenter.current().add(request)
    }

    /// 单行化 + 去控制字符 + 限长。
    static func sanitize(_ raw: String, limit: Int) -> String {
        let collapsed = raw
            .unicodeScalars
            .map { scalar -> Character in
                if scalar.properties.generalCategory == .control { return " " }
                return Character(scalar)
            }
            .reduce(into: "") { $0.append($1) }
            .components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        if collapsed.count <= limit { return collapsed }
        return String(collapsed.prefix(limit - 1)) + "…"
    }
}
