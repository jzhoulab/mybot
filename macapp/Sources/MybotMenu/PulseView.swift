import SwiftUI
import AppKit

/// The morning pulse in the app: the same text as the Discord/markdown copy,
/// split into sections, with one-tap follow-ups that start an agent in hop.
struct PulseSection: Identifiable, Hashable {
    let id: String
    let kind: String      // intro | overlooked | note | article
    let title: String
    let body: String
}

struct PulseAction: Hashable {
    let id: String
    let label: String
    var spawnedTerminal: String?
}

struct Pulse: Identifiable, Hashable {
    var id: String { engine }
    let engine: String
    let label: String
    let date: String
    let title: String
    let sections: [PulseSection]
    var actions: [String: PulseAction]
    let running: Bool
    let stage: String
}

struct PulseClient {
    let config: MybotConfig

    private var baseURL: URL {
        var comps = URLComponents(url: config.guiURL, resolvingAgainstBaseURL: false)!
        comps.path = ""
        return comps.url!
    }

    struct Feed {
        var date = ""                       // the day these pulses are from
        var dates: [String] = []            // every day on file, newest first
        var pulses: [Pulse] = []
        var running: [String: String] = [:] // engine -> stage, while today's is being written
    }

    /// One day's pulses (the newest day when `date` is nil).
    func latest(date: String? = nil, completion: @escaping (Feed) -> Void) {
        var comps = URLComponents(url: baseURL.appendingPathComponent("briefing/latest"), resolvingAgainstBaseURL: false)!
        if let date { comps.queryItems = [URLQueryItem(name: "date", value: date)] }
        var request = URLRequest(url: comps.url!)
        request.timeoutInterval = 15
        MybotHTTP.session.dataTask(with: request) { data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let raw = (obj?["pulses"] as? [[String: Any]]) ?? []
            let pulses = raw.compactMap { p -> Pulse? in
                guard let engine = p["engine"] as? String, let date = p["date"] as? String else { return nil }
                let sections = ((p["sections"] as? [[String: Any]]) ?? []).enumerated().map { index, s in
                    PulseSection(
                        id: (s["id"] as? String) ?? "s\(index)",
                        kind: (s["kind"] as? String) ?? "note",
                        title: (s["title"] as? String) ?? "",
                        body: (s["body"] as? String) ?? "")
                }
                var actions: [String: PulseAction] = [:]
                for a in (p["actions"] as? [[String: Any]]) ?? [] {
                    guard let id = a["id"] as? String else { continue }
                    let spawned = (a["spawned"] as? [String: Any])?["terminal"] as? String
                    actions[id] = PulseAction(id: id, label: (a["label"] as? String) ?? "Follow up",
                                              spawnedTerminal: spawned)
                }
                return Pulse(engine: engine, label: (p["label"] as? String) ?? engine, date: date,
                             title: (p["title"] as? String) ?? "Morning pulse", sections: sections,
                             actions: actions, running: (p["running"] as? Bool) ?? false,
                             stage: (p["stage"] as? String) ?? "")
            }
            let feed = Feed(date: (obj?["date"] as? String) ?? (pulses.first?.date ?? ""),
                            dates: (obj?["dates"] as? [String]) ?? [],
                            pulses: pulses,
                            running: (obj?["running"] as? [String: String]) ?? [:])
            DispatchQueue.main.async { completion(feed) }
        }.resume()
    }

    func figureURL(engine: String, date: String, name: String) -> URL {
        var comps = URLComponents(url: baseURL.appendingPathComponent("briefing/figure"), resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "engine", value: engine), URLQueryItem(name: "date", value: date),
                            URLQueryItem(name: "name", value: name)]
        return comps.url!
    }

    func followUp(engine: String, date: String, actionID: String,
                  completion: @escaping (Result<String, Error>) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("briefing/followup"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 240  // spawning an agent in hop takes a few seconds
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "engine": engine, "date": date, "action_id": actionID,
        ])
        MybotHTTP.session.dataTask(with: request) { data, _, error in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                if let terminal = obj?["terminal"] as? String {
                    completion(.success(terminal))
                } else if (obj?["needs_owner_send"] as? Bool) == true,
                          let message = obj?["message"] as? String,
                          let target = obj?["target"] as? String {
                    // hop only lets the owner type into this session: hand
                    // over the line instead of starting a new agent.
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(message, forType: .string)
                    completion(.success("paste into \(target) (copied)"))
                } else {
                    let message = (obj?["error"] as? String) ?? error?.localizedDescription ?? "follow-up failed"
                    completion(.failure(NSError(domain: "mybot", code: 1,
                                                userInfo: [NSLocalizedDescriptionKey: message])))
                }
            }
        }.resume()
    }
}

/// Pulse markdown rendered as ONE attributed text per section, so a drag
/// selects across paragraphs and bullets (separate Text views per paragraph
/// made selection stop at every paragraph boundary).
enum PulseMarkdown {
    static func blocks(_ text: String) -> [String] {
        var out: [String] = []
        var paragraph: [String] = []
        func flush() {
            if !paragraph.isEmpty { out.append(paragraph.joined(separator: " ")); paragraph = [] }
        }
        for raw in text.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { flush(); continue }
            if line.hasPrefix("- ") || line.hasPrefix("* ") { flush(); out.append(line); continue }
            if raw.hasPrefix("  "), let last = out.last, last.hasPrefix("- ") || last.hasPrefix("* ") {
                out[out.count - 1] = last + " " + line
                continue
            }
            paragraph.append(line)
        }
        flush()
        return out
    }

    static func attributed(_ text: String) -> AttributedString {
        var result = AttributedString()
        for (index, block) in blocks(text).enumerated() {
            if index > 0 { result += AttributedString("\n\n") }
            let isBullet = block.hasPrefix("- ") || block.hasPrefix("* ")
            let body = isBullet ? String(block.dropFirst(2)) : block
            if isBullet { result += AttributedString("•  ") }
            result += (try? AttributedString(
                markdown: body,
                options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(body)
        }
        return result
    }

    /// Copy a section: rich text (keeps bold and links) plus the markdown as
    /// plain text, so it pastes well into both editors and chat boxes.
    static func copy(title: String, body rawBody: String) {
        let body = PulseFigures.asCaptions(rawBody)
        let markdown = title.isEmpty ? body : "## \(title)\n\n\(body)"
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        var rich = AttributedString()
        if !title.isEmpty {
            var heading = AttributedString(title + "\n\n")
            heading.font = .boldSystemFont(ofSize: 14)
            rich += heading
        }
        rich += attributed(body)
        let ns = NSAttributedString(rich)
        if let rtf = try? ns.data(from: NSRange(location: 0, length: ns.length),
                                  documentAttributes: [.documentType: NSAttributedString.DocumentType.rtf]) {
            pasteboard.setData(rtf, forType: .rtf)
        }
        pasteboard.setString(markdown, forType: .string)
    }
}

/// `![caption](figure:name.png)` lines in a pulse: drawings the article
/// writers made, served by the local server (GET /briefing/figure).
enum PulseFigures {
    enum Part: Hashable {
        case text(String)
        case figure(caption: String, name: String)
    }

    private static let line = try! NSRegularExpression(
        pattern: #"^[ \t]*!\[([^\]\n]*)\]\(figure:([A-Za-z0-9][\w.-]{0,80})\)[ \t]*$"#,
        options: [.anchorsMatchLines])

    static func parts(_ text: String) -> [Part] {
        let ns = text as NSString
        var out: [Part] = []
        var pos = 0
        for match in line.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let before = ns.substring(with: NSRange(location: pos, length: match.range.location - pos))
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !before.isEmpty { out.append(.text(before)) }
            out.append(.figure(caption: ns.substring(with: match.range(at: 1)).trimmingCharacters(in: .whitespaces),
                               name: ns.substring(with: match.range(at: 2))))
            pos = match.range.location + match.range.length
        }
        let rest = ns.substring(from: pos).trimmingCharacters(in: .whitespacesAndNewlines)
        if !rest.isEmpty || out.isEmpty { out.append(.text(rest)) }
        return out
    }

    /// For copying: each figure becomes its caption.
    static func asCaptions(_ text: String) -> String {
        parts(text).map {
            switch $0 {
            case .text(let s): return s
            case .figure(let caption, _): return caption.isEmpty ? "" : "*Figure: \(caption)*"
            }
        }.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    static let cache = NSCache<NSString, NSImage>()
}

/// A section's text with its figures drawn in place.
struct PulseBody: View {
    let text: String
    var size: CGFloat = 12
    let engine: String
    let date: String
    let config: MybotConfig

    var body: some View {
        let parts = PulseFigures.parts(text)
        VStack(alignment: .leading, spacing: size * 0.9) {
            ForEach(Array(parts.enumerated()), id: \.offset) { _, part in
                switch part {
                case .text(let s):
                    MarkdownBlock(text: s, size: size)
                case .figure(let caption, let name):
                    PulseFigure(url: PulseClient(config: config).figureURL(engine: engine, date: date, name: name),
                                caption: caption, size: size)
                }
            }
        }
    }
}

struct PulseFigure: View {
    let url: URL
    let caption: String
    var size: CGFloat = 12
    @State private var image: NSImage?
    @State private var failed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Group {
                if let image {
                    Image(nsImage: image)
                        .resizable().interpolation(.high).aspectRatio(contentMode: .fit)
                        .frame(maxWidth: .infinity)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.primary.opacity(0.08)))
                        .onTapGesture(count: 2) { open(image) }
                        .contextMenu {
                            Button("Copy Image") {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.writeObjects([image])
                            }
                            Button("Open in Preview") { open(image) }
                        }
                        .help("Double-click to open it larger")
                } else if failed {
                    Label("Figure unavailable", systemImage: "photo")
                        .font(.system(size: size * 0.85)).foregroundStyle(.tertiary)
                } else {
                    RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.04))
                        .frame(height: 160).overlay(ProgressView().controlSize(.small))
                }
            }
            if !caption.isEmpty {
                Text(caption).font(.system(size: size * 0.85)).foregroundStyle(.secondary)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
        }
        .onAppear(perform: load)
    }

    private func load() {
        if let hit = PulseFigures.cache.object(forKey: url.absoluteString as NSString) { image = hit; return }
        MybotHTTP.session.dataTask(with: url) { data, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            let loaded = ok ? data.flatMap { NSImage(data: $0) } : nil
            DispatchQueue.main.async {
                if let loaded {
                    PulseFigures.cache.setObject(loaded, forKey: url.absoluteString as NSString)
                    image = loaded
                } else {
                    failed = true
                }
            }
        }.resume()
    }

    private func open(_ image: NSImage) {
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(url.lastPathComponent.isEmpty
            ? "pulse-figure.png" : (URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "name" })?.value ?? "pulse-figure.png"))
        try? png.write(to: file)
        NSWorkspace.shared.open(file)
    }
}

struct MarkdownBlock: View {
    let text: String
    var size: CGFloat = 12

    var body: some View {
        Text(PulseMarkdown.attributed(text))
            .font(.system(size: size))
            .lineSpacing(size * 0.28)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

enum PulseDay {
    private static let parser: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.locale = Locale(identifier: "en_US_POSIX"); return f
    }()

    /// "Today", "Yesterday", or "Mon Oct 12" (long: "Monday, Oct 12").
    static func label(_ day: String, long: Bool = false) -> String {
        guard let date = parser.date(from: day) else { return day }
        let cal = Calendar.current
        let name: String
        if cal.isDateInToday(date) { name = "Today" }
        else if cal.isDateInYesterday(date) { name = "Yesterday" }
        else { name = "" }
        let f = DateFormatter(); f.dateFormat = long ? "EEEE, MMM d" : "EEE MMM d"
        if name.isEmpty { return f.string(from: date) }
        return long ? "\(name) — \(f.string(from: date))" : name
    }
}

struct PulseView: View {
    @ObservedObject var model: AppModel
    /// 1 in the menu-bar popover; larger in the reading window.
    var scale: CGFloat = 1
    var inWindow = false
    @Environment(\.openWindow) private var openWindow
    @State private var copied: String?

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.3)
            if let pulse = model.currentPulse {
                ScrollView { sectionsStack(pulse) }
            } else {
                VStack(spacing: 8) {
                    Spacer()
                    Image(systemName: "sun.horizon").font(.system(size: 26)).foregroundStyle(.secondary)
                    Text(model.pulsesLoaded ? "No pulse yet — the first arrives tomorrow morning."
                                            : "Loading…")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                    Spacer()
                }
            }
        }
        .onAppear { model.loadPulses() }
    }

    /// The pulse content without the scroll container (also used by the
    /// offline UI exporter, since ImageRenderer can't draw ScrollView content).
    func sectionsStack(_ pulse: Pulse) -> some View {
        VStack(alignment: .leading, spacing: 16 * scale) {
            ForEach(pulse.sections) { section in
                sectionView(section, pulse: pulse)
            }
        }
        .padding(.horizontal, inWindow ? 34 : 14)
        .padding(.vertical, inWindow ? 24 : 14)
        .frame(maxWidth: inWindow ? 760 : .infinity, alignment: .leading)
        .frame(maxWidth: .infinity)
    }

    private var header: some View {
        HStack(spacing: 8) {
            dayNavigator
            if !model.pulseRunning.isEmpty {
                ProgressView().controlSize(.mini)
                Text("Writing today's pulse…").font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                    .help(model.pulseRunning.map { "\($0.key): \($0.value)" }.sorted().joined(separator: "\n"))
            }
            Spacer()
            if model.pulses.count > 1 {
                Picker("", selection: $model.pulseEngine) {
                    ForEach(model.pulses) { Text($0.label).tag($0.engine) }
                }
                .pickerStyle(.segmented).labelsHidden().frame(width: 130)
            }
            if let pulse = model.currentPulse {
                Button { copyAll(pulse) } label: {
                    Image(systemName: copied == "all" ? "checkmark" : "doc.on.doc")
                }
                .buttonStyle(.plain).help("Copy the whole pulse")
            }
            if !inWindow {
                Button {
                    openWindow(id: "pulse")
                    NSApp.activate(ignoringOtherApps: true)
                } label: { Image(systemName: "macwindow") }
                    .buttonStyle(.plain).help("Open in a reading window")
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    /// ‹ Yesterday ›  — step through earlier pulses, or pick a day.
    private var dayNavigator: some View {
        let size = 12 * min(scale, 1.15)
        return HStack(spacing: 4) {
            Button { model.showPulseDay(offset: 1) } label: { Image(systemName: "chevron.left") }
                .buttonStyle(.plain).disabled(!model.canShowOlderPulse)
                .help("Previous day's pulse")
            Menu {
                ForEach(model.pulseDates, id: \.self) { day in
                    Button(PulseDay.label(day, long: true)) { model.loadPulses(date: day) }
                }
            } label: {
                Text(model.pulseDate.isEmpty ? "Morning pulse" : "\(PulseDay.label(model.pulseDate)) — pulse")
                    .font(.system(size: size, weight: .semibold)).lineLimit(1)
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("Pick a day")
            Button { model.showPulseDay(offset: -1) } label: { Image(systemName: "chevron.right") }
                .buttonStyle(.plain).disabled(!model.canShowNewerPulse)
                .help("Next day's pulse")
        }
        .font(.system(size: size))
    }

    @ViewBuilder
    private func sectionView(_ section: PulseSection, pulse: Pulse) -> some View {
        switch section.kind {
        case "intro":
            sectionBody(section, pulse: pulse, size: 13 * scale)
                .foregroundStyle(.primary)
        case "overlooked":
            VStack(alignment: .leading, spacing: 6) {
                if pulse.sections.first(where: { $0.kind == "overlooked" })?.id == section.id {
                    Text("YOU MAY HAVE OVERLOOKED").font(.system(size: 10 * scale, weight: .heavy)).tracking(0.6)
                        .foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 8) {
                    sectionBody(section, pulse: pulse, size: 12.5 * scale)
                    HStack(spacing: 10) {
                        actionButton(pulse: pulse, id: section.id)
                        Spacer()
                        discussButton(pulse: pulse, section: section)
                        copyButton(key: section.id, title: "", body: section.body)
                    }
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
            }
        case "article":
            VStack(alignment: .leading, spacing: 10) {
                Divider().opacity(0.4)
                Text(section.title).font(.system(size: 15.5 * scale, weight: .bold))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10) {
                    actionButton(pulse: pulse, id: section.id)
                    Spacer()
                    discussButton(pulse: pulse, section: section)
                    copyButton(key: section.id, title: section.title, body: section.body)
                }
                sectionBody(section, pulse: pulse, size: 13 * scale)
            }
        default:
            sectionBody(section, pulse: pulse, size: 11.5 * scale).foregroundStyle(.secondary)
        }
    }

    private func sectionBody(_ section: PulseSection, pulse: Pulse, size: CGFloat) -> some View {
        PulseBody(text: section.body, size: size, engine: pulse.engine, date: pulse.date, config: model.config)
    }

    private func discussButton(pulse: Pulse, section: PulseSection) -> some View {
        Button {
            model.discuss(pulse: pulse, section: section)
            if inWindow { openWindow(id: "ask") }
        } label: {
            Label("Discuss", systemImage: "bubble.left.and.text.bubble.right")
                .font(.system(size: 10.5 * min(scale, 1.15)))
        }
        .buttonStyle(.plain).foregroundStyle(.secondary)
        .help("Ask mybot about this — explain a concept, dig into the idea")
    }

    private func copyButton(key: String, title: String, body: String) -> some View {
        Button {
            PulseMarkdown.copy(title: title, body: body)
            flashCopied(key)
        } label: {
            Label(copied == key ? "Copied" : "Copy", systemImage: copied == key ? "checkmark" : "doc.on.doc")
                .font(.system(size: 10.5 * min(scale, 1.15)))
        }
        .buttonStyle(.plain).foregroundStyle(.secondary)
        .help("Copy this text (keeps formatting and links)")
    }

    private func copyAll(_ pulse: Pulse) {
        var parts: [String] = []
        var overlookedHeader = false
        for section in pulse.sections {
            switch section.kind {
            case "overlooked":
                if !overlookedHeader { parts.append("## You may have overlooked"); overlookedHeader = true }
                parts.append("- " + section.body)
            case "article": parts.append("## \(section.title)\n\n\(section.body)")
            default: parts.append(section.body)
            }
        }
        PulseMarkdown.copy(title: pulse.title, body: parts.joined(separator: "\n\n"))
        flashCopied("all")
    }

    private func flashCopied(_ key: String) {
        copied = key
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { if copied == key { copied = nil } }
    }

    @ViewBuilder
    private func actionButton(pulse: Pulse, id: String) -> some View {
        if let action = pulse.actions[id] {
            if let terminal = action.spawnedTerminal {
                if terminal.hasPrefix("paste into") {
                    Label(terminal.prefix(1).uppercased() + terminal.dropFirst(), systemImage: "doc.on.clipboard")
                        .font(.system(size: 10.5)).foregroundStyle(.orange).lineLimit(1)
                        .help("hop only lets you type into that session. The message is on your clipboard — paste it there.")
                } else {
                    Label("In hop: \(terminal)", systemImage: "checkmark.circle.fill")
                        .font(.system(size: 10.5)).foregroundStyle(.green).lineLimit(1)
                        .help("Sent to the hop session \(terminal).")
                }
            } else if model.pulseLaunching.contains("\(pulse.engine)/\(id)") {
                ProgressView().controlSize(.small)
            } else {
                Button { model.followUp(pulse: pulse, actionID: id) } label: {
                    Label(action.label, systemImage: "arrow.up.forward.app")
                }
                .controlSize(.small)
                .help("Start an agent in hop to follow up on this.")
            }
        }
    }
}
