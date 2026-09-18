import Foundation

public enum XWatchRunnerError: LocalizedError {
    case projectMissing(String)
    case configMissing(String)
    case pythonMissing
    case launchFailed(String)
    case timedOut(Int)
    case noJSON(String)
    case decodeFailed(String)
    case schemaTooNew(Int)

    public var errorDescription: String? {
        switch self {
        case .projectMissing(let path):
            return "找不到 x-watch 项目目录：\(path)"
        case .configMissing(let path):
            return "找不到 config.toml：\(path)"
        case .pythonMissing:
            return "找不到 /usr/bin/python3"
        case .launchFailed(let detail):
            return "启动 x-watch 失败：\(detail)"
        case .timedOut(let seconds):
            return "x-watch 超过 \(seconds) 秒未返回"
        case .noJSON(let tail):
            return "x-watch 没有输出 JSON：\(tail)"
        case .decodeFailed(let detail):
            return "解析 x-watch 输出失败：\(detail)"
        case .schemaTooNew(let schema):
            return "x-watch 的 JSON 契约版本是 \(schema)，高于本 app 支持的 \(TickReport.supportedSchema)，请更新 app"
        }
    }
}

/// 调用 Python 版 x-watch 的包装。
///
/// 为什么不用 Swift 重写采集逻辑：x-watch 那边有 200 个测试覆盖去重、覆盖判定、
/// 429 退避、注入防护等等。这个 app 只做「监督 + 界面 + 通知」，采集引擎保持单一来源。
///
/// ## 两个从 Finder 启动才会踩到的坑
///
/// 1. **GUI app 不继承你的 shell PATH。** 从 Finder / 登录项启动时 PATH 通常只有
///    `/usr/bin:/bin:/usr/sbin:/sbin`，`claude`（在 /opt/homebrew/bin）会找不到。
///    所以这里显式拼一个包含 Homebrew 路径的 PATH 传给子进程。
/// 2. **不能依赖 `python3` 在 PATH 里。** 固定用 `/usr/bin/python3`（macOS 自带）。
///    x-watch 运行时零第三方依赖，系统 Python 3.9 就能跑。
public struct XWatchRunner: Sendable {
    public let projectPath: String
    public let timeoutSeconds: Int

    /// 补进子进程 PATH 的目录。Homebrew 在 Apple Silicon 和 Intel 上路径不同，都带上。
    public static let extraPathEntries = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin"
    ]

    public static let systemPython = "/usr/bin/python3"

    public init(projectPath: String, timeoutSeconds: Int = 900) {
        self.projectPath = projectPath
        self.timeoutSeconds = timeoutSeconds
    }

    public var configPath: String {
        (projectPath as NSString).appendingPathComponent("config.toml")
    }

    private var srcPath: String {
        (projectPath as NSString).appendingPathComponent("src")
    }

    /// 不抛异常的可用性检查。给「首次启动是否自动开始监控」用。
    public var isReady: Bool {
        (try? validate()) != nil
    }

    public func validate() throws {
        let fm = FileManager.default
        guard fm.fileExists(atPath: srcPath) else {
            throw XWatchRunnerError.projectMissing(projectPath)
        }
        guard fm.fileExists(atPath: configPath) else {
            throw XWatchRunnerError.configMissing(configPath)
        }
        guard fm.isExecutableFile(atPath: Self.systemPython) else {
            throw XWatchRunnerError.pythonMissing
        }
    }

    /// 采集一轮并（如果配置开启）判定，返回结构化结果。
    /// - Parameter judge: 覆盖 config.toml 的 [judge].enabled；nil 表示按配置走。
    public func runTick(handles: [String], judge: Bool? = nil) async throws -> TickReport {
        var args = ["-m", "x_watch", "--config", configPath, "run-once", "--json"]
        if let judge { args.append(judge ? "--judge" : "--no-judge") }
        for handle in handles {
            args.append("--handle")
            args.append(handle)
        }
        return try await invoke(args)
    }

    /// 只读状态，不请求上游、不调模型。
    public func fetchStatus() async throws -> TickReport {
        try await invoke(["-m", "x_watch", "--config", configPath, "status", "--json"])
    }

    /// 列出候选帖子但不调模型（不花钱/不耗额度）。返回 stderr 里的人类可读输出。
    public func judgeDryRun(handles: [String]) async throws -> String {
        var args = ["-m", "x_watch", "--config", configPath, "judge", "--dry-run"]
        for handle in handles {
            args.append("--handle")
            args.append(handle)
        }
        let result = try await execute(args)
        return result.stderr.isEmpty ? result.stdout : result.stderr
    }

    // MARK: - 进程执行

    struct ProcessResult {
        let status: Int32
        let stdout: String
        let stderr: String
    }

    private func invoke(_ args: [String]) async throws -> TickReport {
        try validate()
        let result = try await execute(args)

        // x-watch 的退出码 1 表示 partial/failed，此时 JSON 仍然有效且包含错误详情，
        // 所以先尝试解析，而不是直接按失败抛出。
        guard let jsonLine = Self.lastJSONLine(in: result.stdout) else {
            let tail = result.stderr.isEmpty ? result.stdout : result.stderr
            throw XWatchRunnerError.noJSON(String(tail.suffix(400)))
        }

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = Self.isoFormatter.date(from: raw) { return date }
            if let date = Self.isoFormatterNoFraction.date(from: raw) { return date }
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "无法解析时间：\(raw)"
            )
        }

        let report: TickReport
        do {
            report = try decoder.decode(TickReport.self, from: Data(jsonLine.utf8))
        } catch {
            throw XWatchRunnerError.decodeFailed(String(describing: error).prefix(300).description)
        }
        guard report.schema <= TickReport.supportedSchema else {
            throw XWatchRunnerError.schemaTooNew(report.schema)
        }
        return report
    }

    private func execute(_ args: [String]) async throws -> ProcessResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: Self.systemPython)
        process.arguments = args
        process.currentDirectoryURL = URL(fileURLWithPath: projectPath)

        var env = ProcessInfo.processInfo.environment
        // x-watch 运行时零依赖，用 PYTHONPATH 指向 src 即可，无需 pip install
        env["PYTHONPATH"] = srcPath
        env["PYTHONIOENCODING"] = "utf-8"
        // 关键：补 PATH，否则从 Finder 启动时判定后端找不到 claude
        let existing = env["PATH"].map { $0.components(separatedBy: ":") } ?? []
        var merged: [String] = []
        for entry in existing + Self.extraPathEntries where !merged.contains(entry) {
            merged.append(entry)
        }
        env["PATH"] = merged.joined(separator: ":")
        process.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        do {
            try process.run()
        } catch {
            throw XWatchRunnerError.launchFailed(error.localizedDescription)
        }

        // 必须并发读管道：x-watch 的日志很长，等进程结束再读会把 64KB 管道写满而死锁。
        async let outData = Self.readAll(outPipe)
        async let errData = Self.readAll(errPipe)

        let deadline = Date().addingTimeInterval(TimeInterval(timeoutSeconds))
        while process.isRunning {
            if Date() > deadline {
                process.terminate()
                // 给它一点时间体面退出，不行就 SIGKILL
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
                throw XWatchRunnerError.timedOut(timeoutSeconds)
            }
            if Task.isCancelled {
                process.terminate()
                throw CancellationError()
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }

        let stdout = String(data: await outData, encoding: .utf8) ?? ""
        let stderr = String(data: await errData, encoding: .utf8) ?? ""
        return ProcessResult(status: process.terminationStatus, stdout: stdout, stderr: stderr)
    }

    private static func readAll(_ pipe: Pipe) async -> Data {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                continuation.resume(returning: data)
            }
        }
    }

    /// x-watch 把 JSON 写在 stdout 最后一行；日志走 stderr。取最后一个能解析的对象。
    /// 设为 public 以便 `XWatchMonitorCheck --self-test` 直接断言（本机无 XCTest）。
    public static func lastJSONLine(in stdout: String) -> String? {
        let lines = stdout
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { $0.hasPrefix("{") && $0.hasSuffix("}") }
        for line in lines.reversed() {
            if (try? JSONSerialization.jsonObject(with: Data(line.utf8))) != nil {
                return line
            }
        }
        // status --json 用了缩进输出，整段就是一个对象
        let trimmed = stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasPrefix("{"), trimmed.hasSuffix("}"),
           (try? JSONSerialization.jsonObject(with: Data(trimmed.utf8))) != nil {
            return trimmed
        }
        return nil
    }

    private static let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let isoFormatterNoFraction: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()
}
