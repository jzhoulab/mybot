import SwiftUI

enum BotMood { case happy, alert, error, sleepy }

/// A little robot face for the menu bar. The head color + expression encode
/// index health; a red badge shows how many projects await review.
struct BotIcon: View {
    var mood: BotMood
    var accent: Color
    var badge: Int = 0

    private let ink = Color(white: 0.16)
    private let face = Color(white: 0.98)

    var body: some View {
        Canvas { ctx, size in
            let s = size.width / 22.0
            func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x * s, y: y * s) }
            func roundRect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ r: CGFloat) -> Path {
                Path(roundedRect: CGRect(x: x * s, y: y * s, width: w * s, height: h * s), cornerRadius: r * s)
            }

            // antenna
            var stem = Path()
            stem.move(to: pt(11, 5)); stem.addLine(to: pt(11, 2.6))
            ctx.stroke(stem, with: .color(ink), lineWidth: 1.3 * s)
            ctx.fill(Path(ellipseIn: CGRect(x: 9.3 * s, y: 0.4 * s, width: 3.4 * s, height: 3.4 * s)),
                     with: .color(accent))
            ctx.stroke(Path(ellipseIn: CGRect(x: 9.3 * s, y: 0.4 * s, width: 3.4 * s, height: 3.4 * s)),
                       with: .color(ink.opacity(0.5)), lineWidth: 0.7 * s)

            // ears
            for ex in [1.6, 18.8] as [CGFloat] {
                ctx.fill(roundRect(ex, 9.5, 1.9, 4, 0.9), with: .color(accent))
                ctx.stroke(roundRect(ex, 9.5, 1.9, 4, 0.9), with: .color(ink.opacity(0.7)), lineWidth: 0.7 * s)
            }

            // head
            let head = roundRect(3.2, 5, 15.6, 14, 5)
            ctx.fill(head, with: .linearGradient(
                Gradient(colors: [accent, accent.opacity(0.82)]),
                startPoint: pt(11, 5), endPoint: pt(11, 19)))
            ctx.stroke(head, with: .color(ink.opacity(0.85)), lineWidth: 1.1 * s)

            // face plate
            let plate = roundRect(5.4, 8, 11.2, 8, 3.6)
            ctx.fill(plate, with: .color(face))

            drawFace(ctx, s: s)

            // cheeks
            for cx in [6.5, 15.5] as [CGFloat] {
                ctx.fill(Path(ellipseIn: CGRect(x: (cx - 0.9) * s, y: 12.4 * s, width: 1.9 * s, height: 1.3 * s)),
                         with: .color(Color(red: 1, green: 0.5, blue: 0.55).opacity(0.55)))
            }

            // review badge
            if badge > 0 {
                let d: CGFloat = 8.5
                let rect = CGRect(x: (22 - d) * s, y: -0.4 * s, width: d * s, height: d * s)
                ctx.fill(Path(ellipseIn: rect), with: .color(Color(red: 0.92, green: 0.26, blue: 0.3)))
                ctx.stroke(Path(ellipseIn: rect), with: .color(.white), lineWidth: 0.8 * s)
                let label = badge > 9 ? "9+" : "\(badge)"
                let text = Text(label).font(.system(size: 5.6 * s, weight: .heavy)).foregroundColor(.white)
                ctx.draw(ctx.resolve(text), at: CGPoint(x: rect.midX, y: rect.midY))
            }
        }
    }

    private func drawFace(_ ctx: GraphicsContext, s: CGFloat) {
        let eyeY: CGFloat = 11
        let eyes: [CGFloat] = [8.4, 13.6]
        switch mood {
        case .happy, .alert:
            for ex in eyes {
                ctx.fill(Path(ellipseIn: CGRect(x: (ex - 1.1) * s, y: (eyeY - 1.1) * s, width: 2.2 * s, height: 2.2 * s)),
                         with: .color(ink))
                ctx.fill(Path(ellipseIn: CGRect(x: (ex - 0.2) * s, y: (eyeY - 0.9) * s, width: 0.9 * s, height: 0.9 * s)),
                         with: .color(.white))
            }
            var mouth = Path()
            if mood == .happy {
                mouth.move(to: CGPoint(x: 9 * s, y: 13.9 * s))
                mouth.addQuadCurve(to: CGPoint(x: 13 * s, y: 13.9 * s), control: CGPoint(x: 11 * s, y: 15.6 * s))
            } else {
                mouth.addEllipse(in: CGRect(x: 10.2 * s, y: 13.8 * s, width: 1.6 * s, height: 1.6 * s))
            }
            ctx.stroke(mouth, with: .color(ink), lineWidth: 1.0 * s)
        case .error:
            for ex in eyes {
                var x1 = Path(); x1.move(to: CGPoint(x: (ex - 1) * s, y: (eyeY - 1) * s)); x1.addLine(to: CGPoint(x: (ex + 1) * s, y: (eyeY + 1) * s))
                var x2 = Path(); x2.move(to: CGPoint(x: (ex + 1) * s, y: (eyeY - 1) * s)); x2.addLine(to: CGPoint(x: (ex - 1) * s, y: (eyeY + 1) * s))
                ctx.stroke(x1, with: .color(ink), lineWidth: 1.1 * s)
                ctx.stroke(x2, with: .color(ink), lineWidth: 1.1 * s)
            }
            var mouth = Path()
            mouth.move(to: CGPoint(x: 9 * s, y: 15 * s))
            mouth.addQuadCurve(to: CGPoint(x: 13 * s, y: 15 * s), control: CGPoint(x: 11 * s, y: 13.6 * s))
            ctx.stroke(mouth, with: .color(ink), lineWidth: 1.0 * s)
        case .sleepy:
            for ex in eyes {
                var line = Path()
                line.move(to: CGPoint(x: (ex - 1.1) * s, y: eyeY * s)); line.addLine(to: CGPoint(x: (ex + 1.1) * s, y: eyeY * s))
                ctx.stroke(line, with: .color(ink), lineWidth: 1.1 * s)
            }
            let z = Text("z").font(.system(size: 5 * s, weight: .bold)).foregroundColor(ink)
            ctx.draw(ctx.resolve(z), at: CGPoint(x: 15 * s, y: 8 * s))
        }
    }
}

/// Monochrome line-art robot for the menu bar. Rendered as a template image so
/// macOS tints it to match the menu bar (black on light, white on dark) and the
/// selection highlight. Status is conveyed by expression + review badge.
struct BotIconMono: View {
    var mood: BotMood
    var badge: Int = 0
    var ink: Color = .black

    var body: some View {
        Canvas { ctx, size in
            let s = size.width / 22.0
            func stroke(_ path: Path, _ w: CGFloat) { ctx.stroke(path, with: .color(ink), lineWidth: w * s) }
            func fill(_ path: Path) { ctx.fill(path, with: .color(ink)) }
            func roundRect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ r: CGFloat) -> Path {
                Path(roundedRect: CGRect(x: x * s, y: y * s, width: w * s, height: h * s), cornerRadius: r * s)
            }

            // antenna
            var stem = Path(); stem.move(to: CGPoint(x: 11 * s, y: 5 * s)); stem.addLine(to: CGPoint(x: 11 * s, y: 2.6 * s))
            stroke(stem, 1.3)
            fill(Path(ellipseIn: CGRect(x: 9.7 * s, y: 0.9 * s, width: 2.6 * s, height: 2.6 * s)))
            // ears
            fill(roundRect(1.5, 10, 1.9, 3.6, 0.9))
            fill(roundRect(18.6, 10, 1.9, 3.6, 0.9))
            // head (outline)
            stroke(roundRect(3.4, 5, 15.2, 14, 5), 1.5)

            drawFace(ctx, s: s, fill: fill, stroke: stroke)

            if badge > 0 {
                let rect = CGRect(x: 14.8 * s, y: 0.2 * s, width: 6.6 * s, height: 6.6 * s)
                ctx.fill(Path(ellipseIn: rect), with: .color(ink))
                var punch = ctx; punch.blendMode = .destinationOut
                let label = badge > 9 ? "9+" : "\(badge)"
                punch.draw(punch.resolve(Text(label).font(.system(size: 4.6 * s, weight: .black))), at: CGPoint(x: rect.midX, y: rect.midY))
            }
        }
    }

    private func drawFace(_ ctx: GraphicsContext, s: CGFloat,
                          fill: (Path) -> Void, stroke: (Path, CGFloat) -> Void) {
        let eyeY: CGFloat = 11
        let eyes: [CGFloat] = [8.4, 13.6]
        switch mood {
        case .happy, .alert:
            for ex in eyes { fill(Path(ellipseIn: CGRect(x: (ex - 1) * s, y: (eyeY - 1) * s, width: 2 * s, height: 2 * s))) }
            var mouth = Path()
            if mood == .happy {
                mouth.move(to: CGPoint(x: 9.2 * s, y: 14 * s))
                mouth.addQuadCurve(to: CGPoint(x: 12.8 * s, y: 14 * s), control: CGPoint(x: 11 * s, y: 15.5 * s))
            } else {
                mouth.addEllipse(in: CGRect(x: 10.3 * s, y: 13.9 * s, width: 1.4 * s, height: 1.4 * s))
            }
            stroke(mouth, 1.1)
        case .error:
            for ex in eyes {
                var a = Path(); a.move(to: CGPoint(x: (ex - 1) * s, y: (eyeY - 1) * s)); a.addLine(to: CGPoint(x: (ex + 1) * s, y: (eyeY + 1) * s))
                var b = Path(); b.move(to: CGPoint(x: (ex + 1) * s, y: (eyeY - 1) * s)); b.addLine(to: CGPoint(x: (ex - 1) * s, y: (eyeY + 1) * s))
                stroke(a, 1.1); stroke(b, 1.1)
            }
            var mouth = Path(); mouth.move(to: CGPoint(x: 9.2 * s, y: 15 * s)); mouth.addQuadCurve(to: CGPoint(x: 12.8 * s, y: 15 * s), control: CGPoint(x: 11 * s, y: 13.7 * s))
            stroke(mouth, 1.1)
        case .sleepy:
            for ex in eyes {
                var line = Path(); line.move(to: CGPoint(x: (ex - 1.1) * s, y: eyeY * s)); line.addLine(to: CGPoint(x: (ex + 1.1) * s, y: eyeY * s))
                stroke(line, 1.1)
            }
        }
    }
}

/// Renders a preview sheet of all moods on light + dark backgrounds so the icon
/// can be eyeballed without pinning it to the menu bar.
struct IconSheet: View {
    let moods: [(BotMood, Color, Int)] = [
        (.happy, Color(red: 0.30, green: 0.80, blue: 0.46), 0),
        (.alert, Color(red: 0.98, green: 0.74, blue: 0.20), 8),
        (.error, Color(red: 0.95, green: 0.36, blue: 0.36), 0),
        (.sleepy, Color(white: 0.62), 0),
    ]
    var body: some View {
        VStack(spacing: 0) {
            row(bg: Color(white: 0.13))
            row(bg: Color(white: 0.96))
        }
    }
    private func row(bg: Color) -> some View {
        HStack(spacing: 22) {
            ForEach(0..<moods.count, id: \.self) { i in
                VStack(spacing: 6) {
                    BotIcon(mood: moods[i].0, accent: moods[i].1, badge: moods[i].2)
                        .frame(width: 22, height: 22)
                    BotIcon(mood: moods[i].0, accent: moods[i].1, badge: moods[i].2)
                        .frame(width: 44, height: 44)
                }
            }
        }
        .padding(26).frame(maxWidth: .infinity).background(bg)
    }
}

/// The app / Finder icon: a friendly bot on a rounded gradient tile.
struct AppIconView: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 230, style: .continuous)
                .fill(LinearGradient(
                    colors: [Color(red: 0.36, green: 0.80, blue: 0.72),
                             Color(red: 0.22, green: 0.58, blue: 0.86)],
                    startPoint: .topLeading, endPoint: .bottomTrailing))
            BotIcon(mood: .happy, accent: Color(red: 0.98, green: 0.99, blue: 1.0))
                .frame(width: 660, height: 660)
                .shadow(color: .black.opacity(0.18), radius: 26, y: 14)
        }
        .frame(width: 1024, height: 1024)
    }
}

struct MonoSheet: View {
    let moods: [(BotMood, Int)] = [(.happy, 0), (.alert, 8), (.error, 0), (.sleepy, 0)]
    var body: some View {
        VStack(spacing: 0) {
            row(bg: Color(white: 0.13), ink: .white)
            row(bg: Color(white: 0.96), ink: .black)
        }
    }
    private func row(bg: Color, ink: Color) -> some View {
        HStack(spacing: 30) {
            ForEach(0..<moods.count, id: \.self) { i in
                VStack(spacing: 8) {
                    BotIconMono(mood: moods[i].0, badge: moods[i].1, ink: ink).frame(width: 22, height: 22)
                    BotIconMono(mood: moods[i].0, badge: moods[i].1, ink: ink).frame(width: 40, height: 40)
                }
            }
        }
        .padding(26).frame(maxWidth: .infinity).background(bg)
    }
}

enum IconExporter {
    @MainActor private static func write(_ view: some View, width: CGFloat, to path: String) {
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let renderer = ImageRenderer(content: AnyView(view.frame(width: width)))
        renderer.scale = 2
        guard let image = renderer.nsImage,
              let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: URL(fileURLWithPath: path))
    }

    @MainActor static func exportSheet(to path: String = "/tmp/mybot_icons/sheet.png") {
        write(IconSheet(), width: 520, to: path)
        write(MonoSheet(), width: 520, to: "/tmp/mybot_icons/mono.png")
    }

    @MainActor static func exportAppIcon(to path: String = "/tmp/mybot_icons/appicon.png") {
        write(AppIconView(), width: 1024, to: path)
    }
}

#if DEBUG
struct BotIcon_Previews: PreviewProvider {
    static var previews: some View {
        HStack(spacing: 12) {
            BotIcon(mood: .happy, accent: .green).frame(width: 44, height: 44)
            BotIcon(mood: .alert, accent: .orange, badge: 8).frame(width: 44, height: 44)
            BotIcon(mood: .error, accent: .red).frame(width: 44, height: 44)
            BotIcon(mood: .sleepy, accent: .gray).frame(width: 44, height: 44)
        }.padding().background(Color.black)
    }
}
#endif
