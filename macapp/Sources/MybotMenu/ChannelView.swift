import SwiftUI

// MARK: model

struct ChannelMessage: Identifiable, Hashable {
    let id: String
    let seq: Int
    let ts: Date
    let authorId: String
    let authorName: String
    let isBot: Bool
    let isSelf: Bool
    let onBehalf: String     // a bot posted this for its owner ("Jian")
    let text: String
    let replyTo: String
    let relayKind: String    // "ask" | "ans" | ""
    let local: Bool          // a note from the app (never posted)
    var pending = false      // optimistic, not yet echoed by the channel

    /// Who to show as the author: the person a bot posted for, else the author.
    var displayName: String { onBehalf.isEmpty ? authorName : onBehalf }
    var via: String { onBehalf.isEmpty ? "" : authorName }
    var showsAsBot: Bool { isBot && onBehalf.isEmpty }
}

struct ChannelMember: Identifiable, Hashable {
    let id: String
    let name: String
    let isBot: Bool
    let isSelf: Bool
    var blocked: Bool
}

struct ChannelInfo: Identifiable, Hashable {
    var id: String { platform + ":" + channelId }
    let platform: String
    let channelId: String
    let name: String
    let guild: String
}

struct ChannelWaiting: Identifiable, Hashable {
    var id: String { requestId + botId }
    let requestId: String
    let botId: String
    let name: String
}

/// The team channel (one shared Discord/Slack channel) as the menu app sees
/// it: the bridge mirrors it to the server, this polls the server.
final class ChannelModel: ObservableObject {
    @Published var connected: ChannelInfo?
    @Published var available: [ChannelInfo] = []
    @Published var bridges: [String] = []
    @Published var messages: [ChannelMessage] = []
    @Published var members: [ChannelMember] = []
    @Published var waiting: [ChannelWaiting] = []
    @Published var loaded = false
    @Published var error: String?

    private let config: MybotConfig
    private var seq = 0
    private var timer: Timer?
    private var inFlight = false

    init(config: MybotConfig) { self.config = config }

    private func url(_ query: String = "") -> URL {
        var comps = URLComponents(url: config.guiURL, resolvingAgainstBaseURL: false)!
        comps.path = "/channel"
        comps.query = query.isEmpty ? nil : query
        return comps.url!
    }

    func start() {
        refresh(full: true)
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func stop() { timer?.invalidate(); timer = nil }

    func refresh(full: Bool = false) {
        guard !inFlight else { return }
        inFlight = true
        var request = URLRequest(url: url(full ? "" : "after=\(seq)"))
        request.timeoutInterval = 10
        MybotHTTP.session.dataTask(with: request) { [weak self] data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                guard let self else { return }
                self.inFlight = false
                if let obj { self.apply(obj, full: full) }
            }
        }.resume()
    }

    private func apply(_ obj: [String: Any], full: Bool) {
        loaded = true
        let previous = connected
        connected = (obj["connected"] as? [String: Any]).map(Self.channel)
        available = ((obj["available"] as? [[String: Any]]) ?? []).map(Self.channel)
        bridges = ((obj["bridges"] as? [String: Any]) ?? [:]).keys.sorted()
        members = ((obj["members"] as? [[String: Any]]) ?? []).map {
            ChannelMember(id: ($0["id"] as? String) ?? "", name: ($0["name"] as? String) ?? "",
                          isBot: ($0["is_bot"] as? Bool) ?? false, isSelf: ($0["is_self"] as? Bool) ?? false,
                          blocked: ($0["blocked"] as? Bool) ?? false)
        }
        waiting = ((obj["waiting"] as? [[String: Any]]) ?? []).map {
            ChannelWaiting(requestId: ($0["request_id"] as? String) ?? "", botId: ($0["bot_id"] as? String) ?? "",
                           name: ($0["name"] as? String) ?? "")
        }
        let incoming = ((obj["messages"] as? [[String: Any]]) ?? []).map(Self.message)
        if full || previous?.id != connected?.id {
            messages = incoming
        } else if !incoming.isEmpty {
            let known = Set(messages.map(\.id))
            messages.append(contentsOf: incoming.filter { !known.contains($0.id) })
        }
        // An echoed post replaces its optimistic copy.
        let echoed = Set(messages.filter { !$0.pending && !$0.onBehalf.isEmpty }.map(\.text))
        messages.removeAll { $0.pending && (echoed.contains($0.text) || Date().timeIntervalSince($0.ts) > 30) }
        seq = max(seq, (obj["seq"] as? Int) ?? seq)
    }

    private static func channel(_ c: [String: Any]) -> ChannelInfo {
        ChannelInfo(platform: (c["platform"] as? String) ?? "discord",
                    channelId: (c["channel_id"] as? String) ?? (c["id"] as? String) ?? "",
                    name: (c["name"] as? String) ?? "", guild: (c["guild"] as? String) ?? "")
    }

    private static func message(_ m: [String: Any]) -> ChannelMessage {
        ChannelMessage(
            id: (m["id"] as? String) ?? UUID().uuidString, seq: (m["seq"] as? Int) ?? 0,
            ts: Date(timeIntervalSince1970: (m["ts"] as? Double) ?? Date().timeIntervalSince1970),
            authorId: (m["author_id"] as? String) ?? "", authorName: (m["author_name"] as? String) ?? "",
            isBot: (m["is_bot"] as? Bool) ?? false, isSelf: (m["is_self"] as? Bool) ?? false,
            onBehalf: (m["on_behalf"] as? String) ?? "", text: (m["text"] as? String) ?? "",
            replyTo: (m["reply_to"] as? String) ?? "", relayKind: (m["relay_kind"] as? String) ?? "",
            local: (m["local"] as? Bool) ?? false)
    }

    private func post(_ body: [String: Any], completion: (([String: Any]?) -> Void)? = nil) {
        var request = URLRequest(url: url())
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 20
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        MybotHTTP.session.dataTask(with: request) { data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { completion?(obj) }
        }.resume()
    }

    func connect(_ channel: ChannelInfo) {
        connected = channel   // optimistic
        messages = []
        post(["action": "connect", "platform": channel.platform, "channel_id": channel.channelId]) { [weak self] obj in
            guard let self else { return }
            if let obj, (obj["ok"] as? Bool) == true { self.apply(obj, full: true) }
            else { self.error = (obj?["error"] as? String) ?? "Couldn't connect that channel"; self.refresh(full: true) }
        }
    }

    func disconnect() {
        connected = nil
        messages = []
        post(["action": "disconnect"]) { [weak self] obj in if let obj { self?.apply(obj, full: true) } }
    }

    func setBlocked(_ member: ChannelMember, _ blocked: Bool) {
        if let i = members.firstIndex(where: { $0.id == member.id }) { members[i].blocked = blocked }
        post(["action": blocked ? "block" : "unblock", "bot_id": member.id])
    }

    /// Members whose @name is written in the text are the mentions.
    func mentions(in text: String) -> [ChannelMember] {
        members.filter { text.contains("@" + $0.name) }
    }

    func send(_ text: String, ownerName: String, replyTo: String = "") {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        let me = members.first(where: \.isSelf)
        messages.append(ChannelMessage(
            id: "pending-\(UUID().uuidString)", seq: Int.max, ts: Date(), authorId: me?.id ?? "",
            authorName: me?.name ?? "mybot", isBot: true, isSelf: true,
            onBehalf: ownerName.isEmpty ? "You" : ownerName, text: clean, replyTo: replyTo,
            relayKind: "", local: false, pending: true))
        let body: [String: Any] = ["action": "post", "text": clean, "reply_to": replyTo,
                                   "mention_ids": mentions(in: clean).map(\.id)]
        post(body) { [weak self] obj in
            guard let self else { return }
            if (obj?["ok"] as? Bool) != true {
                self.messages.removeAll { $0.pending }
                self.error = (obj?["error"] as? String) ?? "Couldn't post — is the mybot server running?"
            }
            self.refresh()
        }
    }
}

// MARK: view

/// A Slack-like view of the team channel: teammates and their mybots talking
/// in public. @mention a teammate's bot to ask it (a relayed ask, answered in
/// the channel); @mention yours to have it answer there.
struct ChannelView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var channel: ChannelModel
    @State private var input = ""
    @State private var picker = 0
    @State private var replyTo: ChannelMessage?
    @State private var showMembers = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            if let connected = channel.connected {
                header(connected)
                Divider().opacity(0.3)
                messageList
                Divider().opacity(0.3)
                composer
            } else {
                connectList
            }
        }
        .onAppear { channel.start(); focused = true }
        .onDisappear { channel.stop() }
    }

    // MARK: header

    private func header(_ c: ChannelInfo) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "number").font(.system(size: 12, weight: .bold)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 0) {
                Text(c.name.isEmpty ? c.channelId : c.name).font(.system(size: 12.5, weight: .bold)).lineLimit(1)
                Text([c.guild, c.platform.capitalized].filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.system(size: 9.5)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Button { showMembers.toggle() } label: {
                HStack(spacing: 4) {
                    Image(systemName: "person.2").font(.system(size: 10))
                    Text("\(channel.members.count)").font(.system(size: 11, weight: .semibold))
                }
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(Capsule().fill(Color.primary.opacity(0.06)))
            }
            .buttonStyle(.plain)
            .help("People and bots in this channel")
            .popover(isPresented: $showMembers, arrowEdge: .bottom) { membersPopover }
            Menu {
                ForEach(channel.available.filter { $0.id != c.id }) { other in
                    Button("Switch to #\(other.name)" + (other.guild.isEmpty ? "" : " (\(other.guild))")) {
                        channel.connect(other)
                    }
                }
                Divider()
                Button("Disconnect channel") { channel.disconnect() }
            } label: {
                Image(systemName: "ellipsis.circle").font(.system(size: 12))
            }
            .menuStyle(.borderlessButton).fixedSize()
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    private var membersPopover: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("IN THIS CHANNEL").font(.system(size: 9.5, weight: .heavy)).tracking(0.5).foregroundStyle(.secondary)
            ForEach(channel.members) { member in
                HStack(spacing: 8) {
                    ChannelAvatar(name: member.name, isBot: member.isBot, isSelf: member.isSelf, size: 20)
                    Text(member.name).font(.system(size: 11.5, weight: .medium))
                    if member.isBot { BotBadge() }
                    if member.isSelf { Text("yours").font(.system(size: 9.5)).foregroundStyle(.secondary) }
                    Spacer(minLength: 12)
                    if member.isBot && !member.isSelf {
                        Toggle("Answers its asks", isOn: Binding(
                            get: { !member.blocked }, set: { channel.setBlocked(member, !$0) }))
                            .toggleStyle(.switch).controlSize(.mini).labelsHidden()
                            .help(member.blocked
                                  ? "Your bot ignores questions this bot relays"
                                  : "Your bot answers questions this bot relays for its owner (teammate view, in public)")
                    }
                }
            }
            Text("Bots in your team channel can relay their owner's questions to your bot. It answers them in the channel with what teammates may see. Switch one off to ignore it.")
                .font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(12).frame(width: 300)
    }

    // MARK: not connected

    private var connectList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("Team channel").font(.system(size: 13, weight: .bold))
                Text("Pick the shared Discord or Slack channel where your team and their mybots talk. You'll see it here, and you can @mention a teammate's bot to ask it, or yours to have it answer. Everything in the channel is public to its members, so your bot answers there with what teammates may see.")
                    .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if !channel.loaded {
                    ProgressView().controlSize(.small)
                } else if channel.available.isEmpty {
                    Text(channel.bridges.isEmpty
                         ? "No chat bridge has reported in yet. Connect Discord or Slack above, invite the bot to your team's channel, and it will show up here."
                         : "The bot can't post in any channel yet. Invite it to your team's channel; it shows up here within a few minutes.")
                        .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.04)))
                } else {
                    ForEach(channel.available) { c in
                        Button { channel.connect(c) } label: {
                            HStack(spacing: 8) {
                                Image(systemName: "number").font(.system(size: 11, weight: .bold)).foregroundStyle(.secondary)
                                Text(c.name).font(.system(size: 12, weight: .semibold))
                                Text([c.guild, c.platform.capitalized].filter { !$0.isEmpty }.joined(separator: " · "))
                                    .font(.system(size: 10)).foregroundStyle(.secondary)
                                Spacer()
                                Text("Connect").font(.system(size: 11, weight: .semibold)).foregroundStyle(Palette.accent)
                            }
                            .padding(9)
                            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.04)))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                if let error = channel.error {
                    Text(error).font(.system(size: 10.5)).foregroundStyle(Palette.bad)
                }
            }
            .padding(14)
        }
    }

    // MARK: messages

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if channel.messages.isEmpty {
                        Text("No messages yet. Say hello, or @mention a teammate's bot.")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity).padding(.top, 40)
                    }
                    ForEach(Array(channel.messages.enumerated()), id: \.element.id) { index, msg in
                        ChannelRow(
                            msg: msg,
                            grouped: groupedWithPrevious(index),
                            parent: parent(of: msg),
                            members: channel.members,
                            onReply: { replyTo = msg; focused = true },
                            onMention: { name in insertMention(name) }
                        )
                        .id(msg.id)
                    }
                    ForEach(channel.waiting) { w in
                        HStack(spacing: 8) {
                            ChannelAvatar(name: w.name, isBot: true, isSelf: false, size: 22)
                            TypingDots()
                            Text("\(w.name) is answering…").font(.system(size: 10.5)).foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .id("waiting-\(w.id)")
                    }
                    Color.clear.frame(height: 4).id("bottom")
                }
                .padding(.vertical, 6)
            }
            .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            .onChange(of: channel.messages.count) { withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
            .onChange(of: channel.waiting.count) { withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
        }
    }

    private func groupedWithPrevious(_ index: Int) -> Bool {
        guard index > 0 else { return false }
        let prev = channel.messages[index - 1], cur = channel.messages[index]
        return prev.displayName == cur.displayName && prev.via == cur.via && cur.replyTo.isEmpty
            && cur.ts.timeIntervalSince(prev.ts) < 300 && !prev.local && !cur.local
    }

    private func parent(of msg: ChannelMessage) -> ChannelMessage? {
        guard !msg.replyTo.isEmpty else { return nil }
        return channel.messages.first { $0.id == msg.replyTo }
    }

    // MARK: composer

    /// The @word being typed at the end of the input, if any.
    private var mentionQuery: String? {
        guard let at = input.lastIndex(of: "@") else { return nil }
        let word = input[input.index(after: at)...]
        if word.contains(" ") || word.contains("\n") { return nil }
        if at != input.startIndex, !input[input.index(before: at)].isWhitespace { return nil }
        return String(word)
    }

    private var suggestions: [ChannelMember] {
        guard let q = mentionQuery?.lowercased() else { return [] }
        return channel.members.filter { q.isEmpty || $0.name.lowercased().contains(q) }.prefix(6).map { $0 }
    }

    private func pick(_ member: ChannelMember) {
        guard let at = input.lastIndex(of: "@") else { return }
        input = String(input[..<at]) + "@" + member.name + " "
        picker = 0
    }

    private func insertMention(_ name: String) {
        let sep = input.isEmpty || input.hasSuffix(" ") ? "" : " "
        input += sep + "@" + name + " "
        focused = true
    }

    private func send() {
        channel.send(input, ownerName: model.ownerName, replyTo: replyTo?.id ?? "")
        input = ""
        replyTo = nil
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !suggestions.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(suggestions.enumerated()), id: \.element.id) { i, member in
                        Button { pick(member) } label: {
                            HStack(spacing: 8) {
                                ChannelAvatar(name: member.name, isBot: member.isBot, isSelf: member.isSelf, size: 18)
                                Text(member.name).font(.system(size: 11.5, weight: .semibold))
                                if member.isBot { BotBadge() }
                                Spacer()
                                Text(hint(for: member)).font(.system(size: 9.5)).foregroundStyle(.secondary)
                            }
                            .padding(.horizontal, 8).padding(.vertical, 5)
                            .background(RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .fill(i == picker ? Palette.accent.opacity(0.14) : Color.clear))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(4)
                .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(Color(nsColor: .windowBackgroundColor))
                    .shadow(color: .black.opacity(0.15), radius: 6, y: 2))
            }
            if let replyTo {
                HStack(spacing: 6) {
                    Image(systemName: "arrowshape.turn.up.left").font(.system(size: 9))
                    Text("Replying to \(replyTo.displayName): \(replyTo.text)").lineLimit(1)
                    Spacer()
                    Button { self.replyTo = nil } label: { Image(systemName: "xmark").font(.system(size: 9, weight: .bold)) }
                        .buttonStyle(.plain)
                }
                .font(.system(size: 10.5)).foregroundStyle(.secondary)
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Message #\(channel.connected?.name ?? "channel") — type @ to mention a bot", text: $input, axis: .vertical)
                    .textFieldStyle(.plain).font(.system(size: 12.5))
                    .lineLimit(1...6)
                    .focused($focused)
                    .onChange(of: input) { picker = 0 }
                    .onKeyPress(.upArrow) {
                        guard !suggestions.isEmpty else { return .ignored }
                        picker = (picker - 1 + suggestions.count) % suggestions.count
                        return .handled
                    }
                    .onKeyPress(.downArrow) {
                        guard !suggestions.isEmpty else { return .ignored }
                        picker = (picker + 1) % suggestions.count
                        return .handled
                    }
                    .onKeyPress(.tab) {
                        guard !suggestions.isEmpty else { return .ignored }
                        pick(suggestions[min(picker, suggestions.count - 1)])
                        return .handled
                    }
                    .onKeyPress(.return, phases: .down) { press in
                        if !suggestions.isEmpty {
                            pick(suggestions[min(picker, suggestions.count - 1)])
                            return .handled
                        }
                        if press.modifiers.contains(.shift) { return .ignored }   // newline
                        send()
                        return .handled
                    }
                    .onKeyPress(.escape) {
                        if replyTo != nil { replyTo = nil; return .handled }
                        return .ignored
                    }
                Button(action: send) {
                    Image(systemName: "paperplane.fill").font(.system(size: 12))
                        .foregroundStyle(input.trimmingCharacters(in: .whitespaces).isEmpty ? Color.secondary : Palette.accent)
                }
                .buttonStyle(.plain)
                .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty)
                .help("Send (⏎) · new line (⇧⏎)")
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))
            if !channel.mentions(in: input).filter({ $0.isBot }).isEmpty {
                Text(mentionNote).font(.system(size: 9.5)).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
    }

    private func hint(for member: ChannelMember) -> String {
        if member.isSelf { return "your bot answers here" }
        if member.isBot { return "ask on your behalf" }
        return ""
    }

    private var mentionNote: String {
        let bots = channel.mentions(in: input).filter(\.isBot)
        let names = bots.map(\.name).joined(separator: ", ")
        return "Posts as you via your bot. \(names) will answer in the channel, visible to everyone there."
    }
}

// MARK: row

private struct ChannelRow: View {
    let msg: ChannelMessage
    let grouped: Bool
    let parent: ChannelMessage?
    let members: [ChannelMember]
    let onReply: () -> Void
    let onMention: (String) -> Void
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if let parent {
                HStack(spacing: 5) {
                    Image(systemName: "arrow.turn.up.right").font(.system(size: 8)).foregroundStyle(.tertiary)
                    Text("\(parent.displayName): \(parent.text)").lineLimit(1)
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                .padding(.leading, 40)
            }
            HStack(alignment: .top, spacing: 9) {
                if grouped {
                    Color.clear.frame(width: 28, height: 1)
                } else {
                    ChannelAvatar(name: msg.displayName, isBot: msg.showsAsBot, isSelf: msg.isSelf && msg.onBehalf.isEmpty, size: 28)
                }
                VStack(alignment: .leading, spacing: 2) {
                    if !grouped {
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Text(msg.displayName).font(.system(size: 12, weight: .bold))
                                .onTapGesture { onMention(msg.via.isEmpty ? msg.authorName : msg.via) }
                            if msg.showsAsBot { BotBadge() }
                            if !msg.via.isEmpty {
                                Text("via \(msg.via)").font(.system(size: 9.5)).foregroundStyle(.secondary)
                            }
                            Text(msg.ts, format: .dateTime.hour().minute()).font(.system(size: 9.5)).foregroundStyle(.tertiary)
                            if msg.relayKind == "ans" {
                                Image(systemName: "arrowshape.turn.up.backward.badge.clock")
                                    .font(.system(size: 9)).foregroundStyle(Palette.accent)
                                    .help("Answered a question relayed by another bot")
                            }
                        }
                    }
                    MarkdownText(text: linkedMentions(msg.text), size: 12)
                        .foregroundStyle(msg.local ? Color.secondary : Color.primary)
                        .environment(\.openURL, OpenURLAction { url in
                            if url.scheme == "mybot-mention" { onMention(url.host ?? ""); return .handled }
                            return .systemAction
                        })
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 12).padding(.top, grouped ? 1 : 7).padding(.bottom, 1)
        .opacity(msg.pending ? 0.55 : 1)
        .background(hovering ? Color.primary.opacity(0.035) : Color.clear)
        .overlay(alignment: .topTrailing) {
            if hovering && !msg.local && !msg.pending {
                Button(action: onReply) {
                    Image(systemName: "arrowshape.turn.up.left").font(.system(size: 10))
                        .padding(5)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .windowBackgroundColor))
                            .shadow(color: .black.opacity(0.12), radius: 2, y: 1))
                }
                .buttonStyle(.plain).help("Reply").padding(.trailing, 10).padding(.top, 2)
            }
        }
        .onHover { hovering = $0 }
    }

    /// @Name of a channel member renders as a highlighted, clickable mention.
    private func linkedMentions(_ text: String) -> String {
        var out = text
        for m in members.sorted(by: { $0.name.count > $1.name.count }) where !m.name.isEmpty {
            let host = m.name.addingPercentEncoding(withAllowedCharacters: .urlHostAllowed) ?? m.name
            out = out.replacingOccurrences(of: "@" + m.name, with: "[@\(m.name)](mybot-mention://\(host))")
        }
        return out
    }
}

// MARK: bits

struct ChannelAvatar: View {
    let name: String
    let isBot: Bool
    let isSelf: Bool
    var size: CGFloat = 28

    var body: some View {
        Group {
            if isSelf {
                BotAvatar(mood: .happy)
            } else {
                ZStack {
                    RoundedRectangle(cornerRadius: size * 0.28, style: .continuous).fill(color.gradient)
                    if isBot {
                        Image(systemName: "sparkles").font(.system(size: size * 0.45, weight: .semibold))
                    } else {
                        Text(initials).font(.system(size: size * 0.4, weight: .bold))
                    }
                }
                .foregroundStyle(.white)
            }
        }
        .frame(width: size, height: size)
    }

    private var initials: String {
        let parts = name.split(whereSeparator: { $0 == " " || $0 == "-" || $0 == "_" }).prefix(2)
        return parts.compactMap(\.first).map { String($0).uppercased() }.joined()
    }

    private var color: Color {
        let palette: [Color] = [.indigo, .teal, .orange, .pink, .purple, .green, .blue, .brown]
        let hash = name.unicodeScalars.reduce(0) { ($0 &* 31 &+ Int($1.value)) & 0xffff }
        return palette[hash % palette.count]
    }
}

private struct BotBadge: View {
    var body: some View {
        Text("BOT").font(.system(size: 8, weight: .heavy))
            .padding(.horizontal, 4).padding(.vertical, 1)
            .background(RoundedRectangle(cornerRadius: 3).fill(Color.primary.opacity(0.1)))
            .foregroundStyle(.secondary)
    }
}

private struct TypingDots: View {
    @State private var phase = 0
    private let timer = Timer.publish(every: 0.35, on: .main, in: .common).autoconnect()

    var body: some View {
        HStack(spacing: 3) {
            ForEach(0..<3) { i in
                Circle().frame(width: 4, height: 4).opacity(i == phase ? 0.9 : 0.3)
            }
        }
        .foregroundStyle(.secondary)
        .onReceive(timer) { _ in phase = (phase + 1) % 3 }
    }
}
