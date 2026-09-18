// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "XWatchMonitor",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .library(name: "XWatchCore", targets: ["XWatchCore"]),
        .executable(name: "XWatchMonitor", targets: ["XWatchMonitor"]),
        .executable(name: "XWatchMonitorCheck", targets: ["XWatchMonitorCheck"])
    ],
    targets: [
        .target(name: "XWatchCore"),
        .executableTarget(
            name: "XWatchMonitor",
            dependencies: ["XWatchCore"]
        ),
        .executableTarget(
            name: "XWatchMonitorCheck",
            dependencies: ["XWatchCore"]
        )
    ],
    swiftLanguageVersions: [.v5]
)
