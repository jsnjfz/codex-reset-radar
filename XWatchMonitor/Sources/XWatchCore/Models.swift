import Foundation

/// 监控设置。存在 UserDefaults 里（JSON 编码），和 x-watch 的 config.toml 分开：
/// config.toml 管采集策略，这里只管「app 怎么跑」。
public struct MonitorSettings: Codable, Equatable, Sendable {
    /// x-watch 项目根目录（里面要有 config.toml）
    public var projectPath: String
    /// 只监控这些账号；留空表示用 config.toml 里所有启用账号
    public var handlesText: String
    /// 检查间隔（分钟）。reset 监控的端到端延迟 ≈ 这个值。
    /// 默认 20 —— 够及时，又不必每 5 分钟就打一次上游。
    public var intervalMinutes: Int
    /// 概率达到这个值才弹通知
    public var threshold: Double
    /// 是否让 x-watch 调大模型判定（对应 config.toml 的 [judge].enabled）
    public var judgeEnabled: Bool
    /// 正文显示语言。默认中文。
    public var displayLanguage: DisplayLanguage
    public var isEnabled: Bool

    public static let defaultProjectPath = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("side/x-crawl/x-watch").path

    public init(
        projectPath: String = MonitorSettings.defaultProjectPath,
        handlesText: String = "thsottiaux",
        intervalMinutes: Int = 20,
        threshold: Double = 0.7,
        judgeEnabled: Bool = true,
        displayLanguage: DisplayLanguage = .chinese,
        isEnabled: Bool = false
    ) {
        self.projectPath = projectPath
        self.handlesText = handlesText
        self.intervalMinutes = intervalMinutes
        self.threshold = threshold
        self.judgeEnabled = judgeEnabled
        self.displayLanguage = displayLanguage
        self.isEnabled = isEnabled
    }

    /// 逗号/空格/中文逗号分隔，清掉 @ 和非法字符。
    public var handles: [String] {
        handlesText
            .components(separatedBy: CharacterSet(charactersIn: ",，、 \n\t"))
            .map { raw in
                var h = raw.trimmingCharacters(in: .whitespacesAndNewlines)
                if h.hasPrefix("@") { h.removeFirst() }
                if let last = h.components(separatedBy: "/").last, h.contains("/") { h = last }
                return h.filter { $0.isLetter || $0.isNumber || $0 == "_" }.lowercased()
            }
            .filter { !$0.isEmpty && $0.count <= 15 }
    }

    public var isValid: Bool {
        !projectPath.trimmingCharacters(in: .whitespaces).isEmpty
            && (1...1440).contains(intervalMinutes)
            && (0.0...1.0).contains(threshold)
    }
}

// MARK: - x-watch JSON 契约（对应 src/x_watch/report.py）

/// 一条被判定为「疑似重置公告」的帖子。
public struct ResetVerdict: Codable, Identifiable, Equatable, Sendable {
    public let postID: String
    public let authorHandle: String
    public let text: String
    /// 中文译文。nil = 还没翻（界面回落显示原文并标注）。
    public let textZh: String?
    public let url: String
    public let publishedAt: Date?
    public let probability: Double
    public let scope: String
    public let reasoning: String
    public let model: String
    public let judgedAt: Date?
    public let notifiedAt: Date?

    public var id: String { postID }

    enum CodingKeys: String, CodingKey {
        case postID = "post_id"
        case authorHandle = "author_handle"
        case text
        case textZh = "text_zh"
        case url
        case publishedAt = "published_at"
        case probability
        case scope
        case reasoning
        case model
        case judgedAt = "judged_at"
        case notifiedAt = "notified_at"
    }

    public init(
        postID: String,
        authorHandle: String,
        text: String,
        textZh: String? = nil,
        url: String,
        publishedAt: Date?,
        probability: Double,
        scope: String,
        reasoning: String,
        model: String,
        judgedAt: Date? = nil,
        notifiedAt: Date? = nil
    ) {
        self.postID = postID
        self.authorHandle = authorHandle
        self.text = text
        self.textZh = textZh
        self.url = url
        self.publishedAt = publishedAt
        self.probability = probability
        self.scope = scope
        self.reasoning = reasoning
        self.model = model
        self.judgedAt = judgedAt
        self.notifiedAt = notifiedAt
    }

    /// 只接受 https 链接 —— 帖子里的链接是不可信输入。
    public var safeURL: URL? {
        guard let parsed = URL(string: url), parsed.scheme?.lowercased() == "https" else {
            return nil
        }
        return parsed
    }

    /// 按显示语言给出正文。没有译文时回落原文。
    public func displayText(_ language: DisplayLanguage) -> String {
        language.pick(original: text, translated: textZh)
    }

    /// 是否处在「想看中文但还没译文」的回落状态 —— 界面要标注，不能假装是译文。
    public func isFallingBack(to language: DisplayLanguage) -> Bool {
        language == .chinese && (textZh?.isEmpty ?? true) && !text.isEmpty
    }
}

/// 界面正文的显示语言。默认中文，可一键切回英文原文。
public enum DisplayLanguage: String, Codable, Sendable, CaseIterable {
    case chinese
    case original

    public var label: String {
        switch self {
        case .chinese: return "中"
        case .original: return "EN"
        }
    }

    public var help: String {
        switch self {
        case .chinese: return "当前显示中文译文，点击切换为英文原文"
        case .original: return "当前显示英文原文，点击切换为中文译文"
        }
    }

    public var toggled: DisplayLanguage {
        self == .chinese ? .original : .chinese
    }

    public func pick(original: String, translated: String?) -> String {
        guard self == .chinese, let translated, !translated.isEmpty else {
            return original
        }
        return translated
    }
}

public struct AccountState: Codable, Identifiable, Equatable, Sendable {
    public let handle: String
    public let lastAttemptAt: Date?
    public let lastValidResponseAt: Date?
    public let coverageStatus: String?
    public let consecutiveFailures: Int
    public let gapFromAt: Date?

    public var id: String { handle }

    enum CodingKeys: String, CodingKey {
        case handle
        case lastAttemptAt = "last_attempt_at"
        case lastValidResponseAt = "last_valid_response_at"
        case coverageStatus = "coverage_status"
        case consecutiveFailures = "consecutive_failures"
        case gapFromAt = "gap_from_at"
    }

    /// 覆盖结论是 limited/unknown 时，说明本轮没能证明这段时间没遗漏。
    public var coverageIsProven: Bool {
        guard let status = coverageStatus else { return false }
        return !["limited", "unknown"].contains(status)
    }
}

public struct JudgeInfo: Codable, Equatable, Sendable {
    /// config.toml 里 [judge].enabled 的值
    public let enabled: Bool
    /// 本轮实际是否判定（可能被 --judge/--no-judge 覆盖）。缺省时回落到 enabled。
    public let enabledEffective: Bool?
    public let backend: String?
    public let model: String?
    public let threshold: Double?
    public let callsLast24h: Int?
    public let maxCallsPerDay: Int?

    enum CodingKeys: String, CodingKey {
        case enabled
        case enabledEffective = "enabled_effective"
        case backend
        case model
        case threshold
        case callsLast24h = "calls_last_24h"
        case maxCallsPerDay = "max_calls_per_day"
    }

    /// 界面应该显示的判定状态。
    public var isActive: Bool { enabledEffective ?? enabled }
}

public struct CollectionSummary: Codable, Equatable, Sendable {
    public let accounts: Int?
    public let newPosts: Int?
    public let pagesFetched: Int?
    public let worstStatus: String?
    public let abortedReason: String?

    enum CodingKeys: String, CodingKey {
        case accounts
        case newPosts = "new_posts"
        case pagesFetched = "pages_fetched"
        case worstStatus = "worst_status"
        case abortedReason = "aborted_reason"
    }
}

public struct JudgeRunSummary: Codable, Equatable, Sendable {
    public let considered: Int?
    public let judged: Int?
    public let failed: Int?
    public let triggered: Int?
    public let notes: [String]?
}

/// 监控账号最近的一条自有帖子（不只是命中）。
public struct RecentPost: Codable, Identifiable, Equatable, Sendable {
    public let postID: String
    public let monitoredHandle: String
    public let text: String
    /// 中文译文。nil = 还没翻。
    public let textZh: String?
    public let url: String
    public let publishedAt: Date?
    public let postType: String?
    /// nil 表示这条还没被判定过
    public let isReset: Bool?
    public let probability: Double?

    public var id: String { postID }

    enum CodingKeys: String, CodingKey {
        case postID = "post_id"
        case monitoredHandle = "monitored_handle"
        case text
        case textZh = "text_zh"
        case url
        case publishedAt = "published_at"
        case postType = "post_type"
        case isReset = "is_reset"
        case probability
    }

    public var safeURL: URL? {
        guard let parsed = URL(string: url), parsed.scheme?.lowercased() == "https" else {
            return nil
        }
        return parsed
    }

    public func displayText(_ language: DisplayLanguage) -> String {
        language.pick(original: text, translated: textZh)
    }

    public func isFallingBack(to language: DisplayLanguage) -> Bool {
        language == .chinese && (textZh?.isEmpty ?? true) && !text.isEmpty
    }

    public var typeLabel: String {
        switch postType {
        case "original": return "原创"
        case "reply": return "回复"
        case "quote": return "引用"
        default: return "帖子"
        }
    }
}

/// 「今天还会不会重置」的预测。按天 + 输入指纹缓存，见 x-watch 的 forecast.py。
public struct ResetForecast: Codable, Identifiable, Equatable, Sendable {
    public let dateKey: String
    public let handle: String
    public let probability: Double
    public let confidence: String
    public let signals: [String]
    public let reasoning: String
    public let model: String
    public let createdAt: Date?
    /// 今天已经有确认的重置了 —— 此时不该显示预测，而是显示「已重置」
    public let alreadyResetToday: Bool?
    public let cached: Bool?

    public var id: String { dateKey + handle }

    enum CodingKeys: String, CodingKey {
        case dateKey = "date_key"
        case handle
        case probability
        case confidence
        case signals
        case reasoning
        case model
        case createdAt = "created_at"
        case alreadyResetToday = "already_reset_today"
        case cached
    }

    public var hasResetToday: Bool { alreadyResetToday ?? false }

    public var percentText: String {
        String(format: "%.0f%%", probability * 100)
    }

    public var confidenceLabel: String {
        switch confidence {
        case "high": return "置信度高"
        case "medium": return "置信度中"
        default: return "置信度低"
        }
    }
}

/// `x_watch run-once --json` / `status --json` 的输出。
public struct TickReport: Codable, Equatable, Sendable {
    public let schema: Int
    public let generatedAt: Date?
    public let ok: Bool?
    public let exitCode: Int?
    public let judge: JudgeInfo?
    public let accounts: [AccountState]
    public let resets: [ResetVerdict]
    public let collection: CollectionSummary?
    public let judgeRun: JudgeRunSummary?
    public let errors: [String]?
    /// schema 1 里没有这两个字段，所以是可选的
    public let recentPosts: [RecentPost]?
    public let forecasts: [ResetForecast]?
    /// 单实例锁被别的进程占用 → 本轮被跳过。这是正常行为，不是失败。
    public let skipped: Bool?
    public let skippedReason: String?

    enum CodingKeys: String, CodingKey {
        case schema
        case generatedAt = "generated_at"
        case ok
        case exitCode = "exit_code"
        case judge
        case accounts
        case resets
        case collection
        case judgeRun = "judge_run"
        case errors
        case recentPosts = "recent_posts"
        case forecasts
        case skipped
        case skippedReason = "skipped_reason"
    }

    public var wasSkipped: Bool { skipped ?? false }

    /// 本程序能理解的契约版本。x-watch 提高 schema 时这里必须同步。
    /// v2 起有 recent_posts / forecasts；v3 起有 text_zh。
    public static let supportedSchema = 3
}
