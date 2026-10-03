import SwiftUI

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

/// Minimal markdown for pulse text: paragraphs, "- " bullets, and inline
/// bold/italic/links/code via AttributedString.
struct MarkdownBlock: View {
    let text: String
    var size: CGFloat = 12

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                if block.hasPrefix("- ") || block.hasPrefix("* ") {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•").font(.system(size: size))
                        inline(String(block.dropFirst(2)))
                    }
                } else {
                    inline(block)
                }
            }
        }
    }

    private var blocks: [String] {
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

    private func inline(_ s: String) -> some View {
        let attributed = (try? AttributedString(
            markdown: s,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
        return Text(attributed)
            .font(.system(size: size))
            .lineSpacing(2)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct PulseView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.3)
            if let pulse = model.currentPulse {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(pulse.sections) { section in
                            sectionView(section, pulse: pulse)
                        }
                    }
                    .padding(14)
                }
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

    private var header: some View {
        HStack(spacing: 8) {
            Text(model.currentPulse?.title ?? "Morning pulse")
                .font(.system(size: 12, weight: .semibold)).lineLimit(1)
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
            Button { model.loadPulses() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.plain).help("Reload")
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    @ViewBuilder
    private func sectionView(_ section: PulseSection, pulse: Pulse) -> some View {
        switch section.kind {
        case "intro":
            MarkdownBlock(text: section.body, size: 12.5)
                .foregroundStyle(.primary)
        case "overlooked":
            VStack(alignment: .leading, spacing: 6) {
                if pulse.sections.first(where: { $0.kind == "overlooked" })?.id == section.id {
                    Text("YOU MAY HAVE OVERLOOKED").font(.system(size: 10, weight: .heavy)).tracking(0.6)
                        .foregroundStyle(.secondary)
                }
                HStack(alignment: .top, spacing: 8) {
                    MarkdownBlock(text: section.body, size: 12)
                    Spacer(minLength: 0)
                    actionButton(pulse: pulse, id: section.id)
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
            }
        case "article":
            VStack(alignment: .leading, spacing: 8) {
                Divider().opacity(0.4)
                HStack(alignment: .top) {
                    Text(section.title).font(.system(size: 14, weight: .bold))
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 6)
                    actionButton(pulse: pulse, id: section.id)
                }
                MarkdownBlock(text: section.body, size: 12)
            }
        default:
            MarkdownBlock(text: section.body, size: 11).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func actionButton(pulse: Pulse, id: String) -> some View {
        if let action = pulse.actions[id] {
            if let terminal = action.spawnedTerminal {
                Label("In hop: \(terminal)", systemImage: "checkmark.circle.fill")
                    .font(.system(size: 10)).foregroundStyle(.green).lineLimit(1)
                    .help("A follow-up agent is working on this in hop (\(terminal)).")
            } else if model.pulseLaunching.contains("\(pulse.engine)/\(id)") {
                ProgressView().controlSize(.small)
            } else {
                Button(action.label) { model.followUp(pulse: pulse, actionID: id) }
                    .controlSize(.small)
                    .help("Start an agent in hop to follow up on this.")
            }
        }
    }
}
