import SwiftUI

// MARK: model

struct SharingFlag: Identifiable, Hashable {
    var id: String { ref }
    let ref: String
    let title: String
    let reason: String
    var isPrivate: Bool
}

struct SharingSuggestion: Hashable {
    let level: String        // team | owner | exclude
    let confidence: String
    let reason: String
    var flags: [SharingFlag]
}

struct SharingProject: Identifiable, Hashable {
    var id: String { key }
    let key: String
    let source: String
    let cwd: String
    let sessions: Int
    var level: String?       // the owner's decision; nil = not decided (owner-only meanwhile)
    let suggestion: SharingSuggestion?

    var displayName: String {
        let name = (cwd as NSString).lastPathComponent
        return name.isEmpty ? cwd : name
    }
}

enum SharingLevel {
    static let all = ["team", "owner", "exclude"]
    static func label(_ level: String) -> String {
        switch level {
        case "team": return "Team"
        case "owner": return "Only me"
        case "exclude": return "Exclude"
        default: return level
        }
    }
}

/// One session the agent recommends sharing, or one already shared.
struct SharedSession: Identifiable, Hashable {
    var id: String { ref }
    let ref: String
    let title: String
    let cwd: String
    let reason: String
    let confidence: String
    let chunks: Int
    let hop: Bool
    let hopName: String

    init?(_ obj: [String: Any]) {
        guard let ref = obj["ref"] as? String else { return nil }
        self.ref = ref
        let name = (obj["name"] as? String) ?? ""
        title = name.isEmpty ? ((obj["title"] as? String) ?? "") : name
        hopName = (obj["hop_name"] as? String) ?? ""
        cwd = (obj["cwd"] as? String) ?? ""
        reason = (obj["reason"] as? String) ?? ""
        confidence = (obj["confidence"] as? String) ?? ""
        chunks = (obj["chunks"] as? Int) ?? 0
        hop = (obj["hop"] as? Bool) ?? false
    }

    /// Shown only for a session hop knows; nothing for anyone not using hop.
    var hopLabel: String? { hopName.isEmpty ? (hop ? "in hop" : nil) : "hop: \(hopName)" }

    var displayFolder: String { (cwd as NSString).abbreviatingWithTildeInPath }
}

struct SharingState {
    var mode = "reviewed"
    var projects: [SharingProject] = []
    var recommended: [SharedSession] = []
    var shared: [SharedSession] = []
    var reviewing = false
    var reviewDone = 0
    var reviewTotal = 0
}

struct SharingClient {
    let config: MybotConfig

    private var url: URL {
        var comps = URLComponents(url: config.guiURL, resolvingAgainstBaseURL: false)!
        comps.path = "/sharing"
        return comps.url!
    }

    func load(completion: @escaping (SharingState?) -> Void) {
        var request = URLRequest(url: url)
        request.timeoutInterval = 20
        send(request, completion: completion)
    }

    func post(_ body: [String: Any], completion: @escaping (SharingState?) -> Void) {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 60
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        send(request, completion: completion)
    }

    private func send(_ request: URLRequest, completion: @escaping (SharingState?) -> Void) {
        MybotHTTP.session.dataTask(with: request) { data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { completion(obj.map(Self.parse)) }
        }.resume()
    }

    static func parse(_ obj: [String: Any]) -> SharingState {
        var state = SharingState()
        state.mode = (obj["mode"] as? String) ?? "reviewed"
        let reviewer = (obj["reviewer"] as? [String: Any]) ?? [:]
        state.reviewing = (reviewer["running"] as? Bool) ?? false
        state.reviewDone = (reviewer["done"] as? Int) ?? 0
        state.reviewTotal = (reviewer["total"] as? Int) ?? 0
        state.recommended = ((obj["recommended_sessions"] as? [[String: Any]]) ?? []).compactMap(SharedSession.init)
        state.shared = ((obj["shared_sessions"] as? [[String: Any]]) ?? []).compactMap(SharedSession.init)
        state.projects = ((obj["projects"] as? [[String: Any]]) ?? []).compactMap { p in
            guard let key = p["key"] as? String else { return nil }
            var suggestion: SharingSuggestion?
            if let s = p["suggestion"] as? [String: Any], let level = s["level"] as? String {
                let flags = ((s["private_sessions"] as? [[String: Any]]) ?? []).compactMap { f -> SharingFlag? in
                    guard let ref = f["ref"] as? String else { return nil }
                    return SharingFlag(ref: ref, title: (f["title"] as? String) ?? "",
                                       reason: (f["reason"] as? String) ?? "",
                                       isPrivate: (f["private"] as? Bool) ?? false)
                }
                suggestion = SharingSuggestion(level: level, confidence: (s["confidence"] as? String) ?? "",
                                               reason: (s["reason"] as? String) ?? "", flags: flags)
            }
            return SharingProject(key: key, source: (p["source"] as? String) ?? "",
                                  cwd: (p["cwd"] as? String) ?? "", sessions: (p["sessions"] as? Int) ?? 0,
                                  level: p["level"] as? String, suggestion: suggestion)
        }
        return state
    }
}

// MARK: view

/// Which projects teammates may ask your bot about. An agent suggests a level
/// for each project (and flags personal sessions inside work projects); you
/// decide. Until you do, a project stays yours alone.
struct SharingView: View {
    @ObservedObject var model: AppModel

    private var undecided: [SharingProject] { model.sharing.projects.filter { $0.level == nil } }
    private var acceptable: [SharingProject] { undecided.filter { $0.suggestion != nil } }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.3)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    intro
                    recommendedSessions
                    suggestedSection("team", "Suggested for your team")
                    suggestedSection("owner", "Suggested only for you")
                    suggestedSection("exclude", "Suggested to exclude from the index")
                    section("To decide", undecided.filter { $0.suggestion == nil })
                    section("Shared with your team", model.sharing.projects.filter { $0.level == "team" })
                    sharedSessions
                    section("Only you", model.sharing.projects.filter { $0.level == "owner" })
                    section("Excluded", model.sharing.projects.filter { $0.level == "exclude" })
                }
                .padding(12)
            }
        }
        .onAppear { model.loadSharing() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Button { model.showSharing = false } label: { Image(systemName: "chevron.left") }
                .buttonStyle(.plain)
            Text("Team sharing").font(.system(size: 12.5, weight: .semibold))
            Spacer()
            if model.sharing.reviewing {
                ProgressView().controlSize(.mini)
                Text("Reviewing \(model.sharing.reviewDone)/\(model.sharing.reviewTotal)")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            } else {
                Button { model.runSharingReview(all: true) } label: { Image(systemName: "sparkles") }
                    .buttonStyle(.plain).help("Ask the agent to review every undecided project again")
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Which of your projects mybot indexes, and which teammates can ask about. An agent read each project's session titles and your requests and suggests: Team (teammates can ask), Only me (indexed, yours alone), or Exclude (not indexed). Nothing changes until you accept.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !acceptable.isEmpty {
                Button {
                    model.acceptSharingSuggestions(acceptable.map(\.key))
                } label: {
                    Text("Accept all \(acceptable.count) suggestions")
                        .font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Capsule().fill(Palette.accent)).foregroundStyle(.white)
                }
                .buttonStyle(.plain)
                .help("Apply each suggested level, and make the flagged sessions private")
            }
        }
    }

    /// Single sessions worth sharing, whatever folder they started from.
    @ViewBuilder
    private var recommendedSessions: some View {
        let sessions = model.sharing.recommended
        let sure = sessions.filter { $0.confidence == "high" }
        if !sessions.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("RECOMMENDED SESSIONS · \(sessions.count)")
                        .font(.system(size: 9.5, weight: .heavy)).tracking(0.5).foregroundStyle(.secondary)
                    Spacer()
                    if !sure.isEmpty {
                        Button("Share the \(sure.count) high-confidence") {
                            model.shareSessions(sure.map(\.ref), share: true)
                        }
                        .font(.system(size: 10.5, weight: .semibold)).buttonStyle(.plain)
                        .foregroundStyle(Palette.accent)
                    }
                }
                ForEach(sessions) { session in
                    sessionRow(session) {
                        Button("Keep") { model.shareSessions([session.ref], share: false) }
                            .buttonStyle(.plain).font(.system(size: 10.5)).foregroundStyle(.secondary)
                        Button("Share") { model.shareSessions([session.ref], share: true) }
                            .controlSize(.small)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var sharedSessions: some View {
        if !model.sharing.shared.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("SHARED SESSIONS · \(model.sharing.shared.count)")
                    .font(.system(size: 9.5, weight: .heavy)).tracking(0.5).foregroundStyle(.secondary)
                ForEach(model.sharing.shared) { session in
                    sessionRow(session) {
                        Button("Stop sharing") { model.shareSessions([session.ref], share: false) }
                            .buttonStyle(.plain).font(.system(size: 10.5)).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private func sessionRow<Actions: View>(_ session: SharedSession, @ViewBuilder actions: () -> Actions) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 7) {
                Text(session.title.isEmpty ? session.ref : session.title)
                    .font(.system(size: 11.5, weight: .medium)).lineLimit(1).help(session.ref)
                Spacer()
                actions()
            }
            Text(([session.hopLabel, session.displayFolder, session.chunks > 0 ? "\(session.chunks) chunks" : nil] as [String?])
                .compactMap { $0 }.joined(separator: " · "))
                .font(.system(size: 9.5)).foregroundStyle(.tertiary).lineLimit(1)
            if !session.reason.isEmpty {
                Text(session.reason).font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(9)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.04)))
    }

    /// Undecided projects the agent put at one level, adoptable in one click.
    @ViewBuilder
    private func suggestedSection(_ level: String, _ title: String) -> some View {
        let projects = undecided.filter { $0.suggestion?.level == level }
        if !projects.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("\(title.uppercased()) · \(projects.count)")
                        .font(.system(size: 9.5, weight: .heavy)).tracking(0.5).foregroundStyle(.secondary)
                    Spacer()
                    Button("Accept these \(projects.count)") {
                        model.acceptSharingSuggestions(projects.map(\.key))
                    }
                    .font(.system(size: 10.5, weight: .semibold)).buttonStyle(.plain)
                    .foregroundStyle(Palette.accent)
                    .help("Apply this suggestion to every project in this group")
                }
                ForEach(projects) { row($0) }
            }
        }
    }

    @ViewBuilder
    private func section(_ title: String, _ projects: [SharingProject]) -> some View {
        if !projects.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("\(title.uppercased()) · \(projects.count)")
                    .font(.system(size: 9.5, weight: .heavy)).tracking(0.5).foregroundStyle(.secondary)
                ForEach(projects) { row($0) }
            }
        }
    }

    private func row(_ project: SharingProject) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 7) {
                SharingSourceDot(source: project.source)
                Text(project.displayName).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                    .help(project.cwd)
                Text("· \(project.sessions)").font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer()
                Picker("", selection: Binding(
                    get: { project.level ?? "" },
                    set: { model.decideSharing(project.key, level: $0) })) {
                    if project.level == nil { Text("—").tag("") }
                    ForEach(SharingLevel.all, id: \.self) { Text(SharingLevel.label($0)).tag($0) }
                }
                .pickerStyle(.segmented).labelsHidden().frame(width: 190)
            }
            if let suggestion = project.suggestion {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Image(systemName: "sparkles").font(.system(size: 9)).foregroundStyle(Palette.accent)
                    Text("Suggested \(SharingLevel.label(suggestion.level))")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(project.level == nil ? Palette.accent : .secondary)
                    Text(suggestion.reason).font(.system(size: 10)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(suggestion.flags) { flag in
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        Image(systemName: "lock").font(.system(size: 9)).foregroundStyle(.orange)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(flag.title.isEmpty ? flag.ref : flag.title)
                                .font(.system(size: 10.5)).lineLimit(1)
                            Text(flag.reason).font(.system(size: 9.5)).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Toggle("Private", isOn: Binding(
                            get: { flag.isPrivate },
                            set: { model.setSessionPrivate(flag.ref, isPrivate: $0) }))
                            .toggleStyle(.checkbox).font(.system(size: 10))
                            .help("Keep this one session to yourself even if the project is shared")
                    }
                    .padding(.leading, 14)
                }
            }
        }
        .padding(9)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.04)))
    }
}

private struct SharingSourceDot: View {
    let source: String
    var body: some View {
        Circle().fill(source == "codex" ? Color.purple.opacity(0.7) : Color.orange.opacity(0.75))
            .frame(width: 7, height: 7).help(source)
    }
}
