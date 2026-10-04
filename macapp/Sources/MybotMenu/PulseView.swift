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

    func latest(completion: @escaping ([Pulse]) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("briefing/latest"))
        request.timeoutInterval = 15
        URLSession.shared.dataTask(with: request) { data, _, _ in
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
            DispatchQueue.main.async { completion(pulses) }
        }.resume()
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
        URLSession.shared.dataTask(with: request) { data, _, error in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                if let terminal = obj?["terminal"] as? String {
                    completion(.success(terminal))
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
    static func copy(title: String, body: String) {
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
            Text(model.currentPulse?.title ?? "Morning pulse")
                .font(.system(size: 12 * min(scale, 1.15), weight: .semibold)).lineLimit(1)
            if let pulse = model.currentPulse, pulse.running {
                ProgressView().controlSize(.mini)
                Text(pulse.stage).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
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
            Button { model.loadPulses() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.plain).help("Reload")
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    @ViewBuilder
    private func sectionView(_ section: PulseSection, pulse: Pulse) -> some View {
        switch section.kind {
        case "intro":
            MarkdownBlock(text: section.body, size: 13 * scale)
                .foregroundStyle(.primary)
        case "overlooked":
            VStack(alignment: .leading, spacing: 6) {
                if pulse.sections.first(where: { $0.kind == "overlooked" })?.id == section.id {
                    Text("YOU MAY HAVE OVERLOOKED").font(.system(size: 10 * scale, weight: .heavy)).tracking(0.6)
                        .foregroundStyle(.secondary)
                }
                VStack(alignment: .leading, spacing: 8) {
                    MarkdownBlock(text: section.body, size: 12.5 * scale)
                    HStack(spacing: 10) {
                        actionButton(pulse: pulse, id: section.id)
                        Spacer()
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
                    copyButton(key: section.id, title: section.title, body: section.body)
                }
                MarkdownBlock(text: section.body, size: 13 * scale)
            }
        default:
            MarkdownBlock(text: section.body, size: 11.5 * scale).foregroundStyle(.secondary)
        }
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
                Label("In hop: \(terminal)", systemImage: "checkmark.circle.fill")
                    .font(.system(size: 10.5)).foregroundStyle(.green).lineLimit(1)
                    .help("A follow-up agent is working on this in hop (\(terminal)).")
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
