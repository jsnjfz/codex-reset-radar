import AppKit
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self

        // 支持从命令行开启/关闭开机启动：
        //   open -a "X 重置监控" --args --enable-login-item
        // SMAppService 只能由 .app 自己调用，所以这个入口留在 app 里。
        let args = ProcessInfo.processInfo.arguments
        if args.contains("--enable-login-item") {
            let error = LoginItemService.setEnabled(true)
            NSLog("[x-watch] 开机启动 → \(error ?? LoginItemService.statusDescription)")
        } else if args.contains("--disable-login-item") {
            let error = LoginItemService.setEnabled(false)
            NSLog("[x-watch] 关闭开机启动 → \(error ?? LoginItemService.statusDescription)")
        }
        // 每次启动都刷新状态文件，方便安装脚本核对
        LoginItemService.writeStatusFile()
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        defer { completionHandler() }
        guard
            let rawURL = response.notification.request.content.userInfo["url"] as? String,
            let url = URL(string: rawURL),
            // userInfo 里的链接来自帖子数据，再校验一次协议
            url.scheme?.lowercased() == "https"
        else {
            return
        }
        NSWorkspace.shared.open(url)
    }
}
