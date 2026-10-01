import SwiftUI
import CoreLocation

/// 「見る」: 即カメラ（AR）。位置＋カメラ方位を API に送り、候補 2〜5 件から選ばせる。
struct LookView: View {
    @StateObject private var location = LocationService()
    @StateObject private var motion = MotionService()
    @StateObject private var ar = ARSessionController()
    @State private var response: CandidatesResponse?
    @State private var selected: BuildingDTO?
    @State private var error: String?
    @State private var lastQueryAt = Date.distantPast
    @State private var measured: Float?
    var onShowMap: () -> Void = {}

    /// AR のカメラ姿勢を優先、無ければ CoreMotion
    private var pose: CameraPose? { (ar.mode == .geo || ar.mode == .worldHeading) ? (ar.cameraPose ?? motion.pose) : motion.pose }

    var body: some View {
        ZStack(alignment: .bottom) {
            if ar.mode == .geo || ar.mode == .worldHeading {
                ARViewContainer(controller: ar).ignoresSafeArea()
                    .onTapGesture { p in measured = ar.distance(atScreenPoint: p); Task { await query(force: true) } }
            } else {
                LinearGradient(colors: [.black, .gray], startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            }
            VStack(spacing: 8) {
                hud
                Spacer()
                candidateSheet
            }
        }
        .task {
            location.start()
            motion.headingAccuracyProvider = { [weak location] in location?.fix?.headingAccuracy }
            motion.start()
            await ar.start(location: location.fix.map { CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude) }, hasHeading: true)
        }
        .onReceive(Timer.publish(every: 2, on: .main, in: .common).autoconnect()) { _ in Task { await query() } }
        .onDisappear { location.stop(); motion.stop(); ar.pause() }
        .sheet(item: $selected) { b in BuildingDetailView(buildingId: b.id) }
    }

    private var hud: some View {
        VStack(spacing: 4) {
            HStack {
                Text(pose.map { "方位 \(Int($0.heading))°" } ?? "方位 取得中")
                Text(location.fix.map { "精度 ±\(Int($0.horizontalAccuracy))m" } ?? "現在地 取得中")
                Spacer()
                Button("地図で選ぶ", action: onShowMap)
            }
            .font(.caption).padding(8).background(.ultraThinMaterial, in: Capsule())
            ForEach(ar.notes + [location.fallbackMessage].compactMap { $0 }, id: \.self) { n in
                Text(n).font(.caption).padding(6).background(Color.orange.opacity(0.85), in: RoundedRectangle(cornerRadius: 8)).foregroundStyle(.white)
            }
        }.padding(.horizontal)
    }

    @ViewBuilder private var candidateSheet: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let error { Text(error).foregroundStyle(.orange) }
            if let sel = response?.selection {
                Text(sel.mode == "suggest" ? "この建物ですか？" : sel.message).font(.headline)
                if response?.sources.contains(where: { $0.mode == "demo" }) == true {
                    Text("デモ建物データ（実在の建物ではありません）").font(.caption).padding(4).background(Color.yellow.opacity(0.3))
                }
                ForEach(Array(sel.candidates.enumerated()), id: \.element.building.id) { i, c in
                    Button { selected = c.building } label: {
                        HStack {
                            Text("\(i + 1)").bold().frame(width: 28)
                            VStack(alignment: .leading) {
                                Text([c.building.usage ?? "建物", c.building.floorsAbove.map { "\($0)階" }].compactMap { $0 }.joined(separator: "・"))
                                Text("約\(Int(c.distanceM))m・" + (c.reasons.first ?? "")).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                        }
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 12).stroke(sel.primaryId == c.building.id ? Color.teal : Color.gray.opacity(0.3), lineWidth: sel.primaryId == c.building.id ? 2 : 1))
                    }.buttonStyle(.plain)
                }
            } else if let a = response?.assessment, !a.usable {
                Text(a.message ?? "現在地を取得できません。")
                Button("地図から選ぶ", action: onShowMap).buttonStyle(.borderedProminent)
            } else {
                Text("建物を探しています…").foregroundStyle(.secondary)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private func query(force: Bool = false) async {
        guard force || Date().timeIntervalSince(lastQueryAt) > 1.5 else { return }
        lastQueryAt = Date()
        do {
            response = try await APIClient.shared.candidates(fix: location.fix, pose: pose, permission: location.permission, measuredDistanceM: measured)
            error = nil
        } catch {
            self.error = "通信できませんでした。地図または保存した家をご利用ください。"
        }
    }
}
