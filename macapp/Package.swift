// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "MybotMenu",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "MybotMenu",
            path: "Sources/MybotMenu",
            linkerSettings: [.linkedLibrary("sqlite3")]
        )
    ]
)
