import Foundation
import ServiceManagement

/// 开机启动（登录项）。
///
/// 用 macOS 13+ 的 `SMAppService.mainApp`，而不是 `osascript` 去操作 System Events：
/// - 会出现在「系统设置 → 通用 → 登录项」里，用户随时能自己关掉。
/// - 不需要申请「自动化」权限，也不会弹 System Events 授权框。
///
/// ## 注意：登录项记录的是**当前这个 .app 的路径**
///
/// 如果之后把 .app 移动或删除，登录项会失效（状态变 `notFound`）。
/// `scripts/build-app.sh` 每次都会 `rm -rf` 掉 `dist/` 里的旧包，所以**用于开机启动的
/// 副本应该放在 `/Applications`**，而不是 `dist/`。
enum LoginItemService {
    /// 当前登录项状态。
    static var status: SMAppService.Status {
        SMAppService.mainApp.status
    }

    static var isEnabled: Bool {
        status == .enabled
    }

    /// 人类可读的状态说明。把 `requiresApproval` 这种「已登记但等用户批准」的中间态说清楚，
    /// 而不是简单显示成「已开启」。
    static var statusDescription: String {
        switch status {
        case .enabled:
            return "已开启：登录后自动启动"
        case .notRegistered:
            return "未开启"
        case .requiresApproval:
            return "已登记，但需要你在「系统设置 → 通用 → 登录项」里批准"
        case .notFound:
            return "登录项失效（.app 可能被移动或删除，重新开启一次即可）"
        @unknown default:
            return "状态未知"
        }
    }

    /// 当前 .app 是否在一个适合做开机启动的位置。
    ///
    /// `dist/` 会被构建脚本清空重建，不适合长期指向。
    static var isInStableLocation: Bool {
        let path = Bundle.main.bundlePath
        return path.hasPrefix("/Applications/") || path.hasPrefix(NSHomeDirectory() + "/Applications/")
    }

    static var locationWarning: String? {
        guard !isInStableLocation else { return nil }
        return "当前运行的是 \(Bundle.main.bundlePath)。"
            + "构建脚本每次会清空 dist/，建议把 .app 拷到 /Applications 再开启开机启动。"
    }

    /// 状态文件路径。app 每次启动都会刷新它，让安装脚本能不靠 `log show` 就查到结果
    /// （`log show` 扫全系统日志，慢到几分钟）。
    static var statusFileURL: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("XWatchMonitor", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base.appendingPathComponent("login-status.txt")
    }

    /// 把当前状态写到状态文件。失败无所谓 —— 这只是给脚本看的。
    static func writeStatusFile() {
        let lines = [
            "bundle_path=\(Bundle.main.bundlePath)",
            "status=\(status.rawValue)",
            "enabled=\(isEnabled)",
            "description=\(statusDescription)",
            "stable_location=\(isInStableLocation)",
            "written_at=\(ISO8601DateFormatter().string(from: Date()))"
        ]
        try? lines.joined(separator: "\n").appending("\n")
            .write(to: statusFileURL, atomically: true, encoding: .utf8)
    }

    /// 开启或关闭。返回 nil 表示成功，否则返回错误说明。
    @discardableResult
    static func setEnabled(_ enabled: Bool) -> String? {
        do {
            if enabled {
                // 已经是 enabled 时再 register 会抛错，先判一下
                guard status != .enabled else { return nil }
                try SMAppService.mainApp.register()
            } else {
                guard status != .notRegistered else { return nil }
                try SMAppService.mainApp.unregister()
            }
            writeStatusFile()
            return nil
        } catch {
            writeStatusFile()
            return "设置开机启动失败：\(error.localizedDescription)"
        }
    }
}
