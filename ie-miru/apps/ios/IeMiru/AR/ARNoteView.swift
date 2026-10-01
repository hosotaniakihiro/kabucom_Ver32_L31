import SwiftUI
import ARKit
import RealityKit
import CoreLocation

struct ARNoteDTO: Codable, Identifiable {
    var id: String
    var buildingId: String
    var status: String
    var text: String
    var foundOn: String
    var anchor: AnchorDTO

    struct AnchorDTO: Codable {
        struct GPS: Codable { var lat: Double; var lng: Double; var accuracyM: Double?; var altitudeM: Double? }
        struct Pose: Codable { var heading: Double; var pitch: Double; var roll: Double; var headingAccuracy: Double? }
        struct Target: Codable { var bearingDeg: Double; var elevationDeg: Double; var distanceM: Double?; var distanceSource: String }
        struct Geo: Codable { var lat: Double; var lng: Double; var altitudeM: Double }
        struct WorldMap: Codable { var key: String; var x: Double; var y: Double; var z: Double }
        var gps: GPS
        var cameraPose: Pose
        var target: Target
        var relativeToBuilding: [String: Double]?
        var geoAnchor: Geo?
        var worldMap: WorldMap?
        var inspectionId: String?
    }

    static let statusLabels: [(String, String)] = [("needs_repair", "修理必要"), ("check", "確認"), ("done", "完了"), ("needs_quote", "要見積")]
    var statusLabel: String { Self.statusLabels.first { $0.0 == status }?.1 ?? status }
}

/// ARメモ。再配置の優先順位: ARWorldMap（同じ場所で再ローカライズ）→ ARGeoAnchor → GPS＋方位推定。
@MainActor
final class ARNoteController: NSObject, ObservableObject, ARSessionDelegate {
    @Published var notes: [ARNoteDTO] = []
    @Published var info = "AR を準備中…"
    let arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
    private var placedIds = Set<String>()
    let location = LocationService()

    override init() {
        super.init()
        arView.session.delegate = self
    }

    func start(buildingId: String) async {
        location.start()
        await load(buildingId: buildingId)
        let config = ARSessionController.worldConfig()
        // 最新の ARWorldMap があれば読み込んで再ローカライズを試みる
        if let withMap = notes.first(where: { $0.anchor.worldMap != nil }),
           let data = try? await APIClient.shared.send("v1/ar-notes/\(withMap.id)/world-map", method: "GET", json: Optional<String>.none),
           let map = try? NSKeyedUnarchiver.unarchivedObject(ofClass: ARWorldMap.self, from: data) {
            config.initialWorldMap = map
            info = "前回の空間を探しています。保存したときと同じ位置からゆっくり見回してください。"
        } else {
            info = "ARWorldMap がないため、GPS と方位で推定表示します（数m の誤差）。"
        }
        arView.session.run(config, options: [.resetTracking, .removeExistingAnchors])
        try? await Task.sleep(nanoseconds: 8_000_000_000)
        placeFallbackPins()
    }

    func load(buildingId: String) async {
        struct R: Decodable { var notes: [ARNoteDTO] }
        let q = buildingId.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? buildingId
        if let d = try? await APIClient.shared.send("v1/ar-notes?buildingId=\(q)", method: "GET", json: Optional<String>.none),
           let r = try? JSONDecoder().decode(R.self, from: d) { notes = r.notes }
    }

    /// 画面中央を raycast してメモを置く（LiDAR 端末はメッシュに当たるので高精度）
    func place(buildingId: String, status: String, text: String) async {
        guard let frame = arView.session.currentFrame, let fix = location.fix else { info = "現在地を取得中です"; return }
        let center = CGPoint(x: arView.bounds.midX, y: arView.bounds.midY)
        let q = arView.makeRaycastQuery(from: center, allowing: .estimatedPlane, alignment: .any)
        let hit = q.flatMap { arView.session.raycast($0).first }
        let pose = ARSessionController.pose(fromARKit: frame.camera.transform)
        let cam = frame.camera.transform.columns.3
        var distance: Double?
        var world: SIMD3<Float>?
        if let h = hit {
            let p = h.worldTransform.columns.3
            world = SIMD3(p.x, p.y, p.z)
            distance = Double(simd_distance(SIMD3(cam.x, cam.y, cam.z), world!)) * cos(pose.pitch * .pi / 180)
        }
        let id = UUID().uuidString
        let fmt = DateFormatter(); fmt.dateFormat = "yyyy-MM-dd"
        var anchor = ARNoteDTO.AnchorDTO(
            gps: .init(lat: fix.latitude, lng: fix.longitude, accuracyM: fix.horizontalAccuracy, altitudeM: fix.altitude),
            cameraPose: .init(heading: pose.heading, pitch: pose.pitch, roll: pose.roll, headingAccuracy: pose.headingAccuracy),
            target: .init(bearingDeg: pose.heading, elevationDeg: pose.pitch, distanceM: distance, distanceSource: hit == nil ? "none" : (ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) ? "lidar" : "ar_raycast")),
            relativeToBuilding: nil, geoAnchor: nil,
            worldMap: world.map { .init(key: "", x: Double($0.x), y: Double($0.y), z: Double($0.z)) }, inspectionId: nil)
        if let d = distance {
            let c = GeoMath.destination(CLLocationCoordinate2D(latitude: fix.latitude, longitude: fix.longitude), bearing: pose.heading, distance: d)
            anchor.geoAnchor = .init(lat: c.latitude, lng: c.longitude, altitudeM: (fix.altitude ?? 0) + 1.5 + d * tan(pose.pitch * .pi / 180))
        }
        struct Body: Encodable { var buildingId: String; var status: String; var text: String; var foundOn: String; var anchor: ARNoteDTO.AnchorDTO }
        struct R: Decodable { var note: ARNoteDTO }
        guard let data = try? await APIClient.shared.send("v1/ar-notes", method: "POST", json: Body(buildingId: buildingId, status: status, text: text, foundOn: fmt.string(from: Date()), anchor: anchor)),
              let created = try? JSONDecoder().decode(R.self, from: data).note else { info = "保存できませんでした"; return }
        if let h = hit {
            let a = ARAnchor(name: created.id, transform: h.worldTransform)
            arView.session.add(anchor: a)
        }
        notes.insert(created, at: 0)
        // 空間地図を保存（同じ場所での再ローカライズ用）
        arView.session.getCurrentWorldMap { map, _ in
            guard let map, let blob = try? NSKeyedArchiver.archivedData(withRootObject: map, requiringSecureCoding: true) else { return }
            Task { _ = try? await APIClient.shared.upload("v1/ar-notes/\(created.id)/world-map", multipart: blob, boundary: "") }
        }
        _ = id
    }

    /// ARWorldMap で復元できなかったメモを GPS＋方位から推定配置
    private func placeFallbackPins() {
        guard let fix = location.fix, let frame = arView.session.currentFrame else { return }
        let here = CLLocationCoordinate2D(latitude: fix.latitude, longitude: fix.longitude)
        let cam = frame.camera.transform.columns.3
        for n in notes where !placedIds.contains(n.id) {
            let target: CLLocationCoordinate2D
            if let g = n.anchor.geoAnchor { target = CLLocationCoordinate2D(latitude: g.lat, longitude: g.lng) }
            else if let d = n.anchor.target.distanceM { target = GeoMath.destination(CLLocationCoordinate2D(latitude: n.anchor.gps.lat, longitude: n.anchor.gps.lng), bearing: n.anchor.target.bearingDeg, distance: d) }
            else { continue }
            let d = GeoMath.distance(here, target), b = GeoMath.bearing(here, target) * .pi / 180
            // gravityAndHeading: +X 東, -Z 北
            let pos = SIMD3<Float>(cam.x + Float(d * sin(b)), cam.y, cam.z - Float(d * cos(b)))
            addPin(for: n, at: pos, estimated: true)
        }
        if !notes.isEmpty { info = "GPS と方位から推定した位置に表示しています（数m の誤差）。" }
    }

    private func addPin(for n: ARNoteDTO, at pos: SIMD3<Float>, estimated: Bool) {
        placedIds.insert(n.id)
        let anchor = AnchorEntity(world: pos)
        let text = ModelEntity(mesh: .generateText("\(n.statusLabel)\n\(n.foundOn.replacingOccurrences(of: "-", with: "/")) 発見\n\(n.text)\(estimated ? "\n(推定位置)" : "")", extrusionDepth: 0.01, font: .systemFont(ofSize: 0.12), containerFrame: .zero, alignment: .center, lineBreakMode: .byWordWrapping),
                               materials: [SimpleMaterial(color: n.status == "done" ? .green : .red, isMetallic: false)])
        anchor.addChild(text)
        arView.scene.addAnchor(anchor)
    }

    nonisolated func session(_ session: ARSession, didAdd anchors: [ARAnchor]) {
        Task { @MainActor in
            for a in anchors {
                guard let name = a.name, let n = self.notes.first(where: { $0.id == name }), !self.placedIds.contains(name) else { continue }
                let p = a.transform.columns.3
                self.addPin(for: n, at: SIMD3(p.x, p.y, p.z), estimated: false)
                self.info = "前回の空間に再ローカライズしました。"
            }
        }
    }
}

struct ARNoteView: View {
    let building: BuildingDTO
    @StateObject private var c = ARNoteController()
    @State private var status = "needs_repair"
    @State private var text = ""

    var body: some View {
        ZStack(alignment: .bottom) {
            ARNoteContainer(view: c.arView).ignoresSafeArea()
            Circle().stroke(.white, lineWidth: 2).frame(width: 44, height: 44).frame(maxHeight: .infinity)
            VStack(alignment: .leading, spacing: 8) {
                Text(c.info).font(.caption)
                Picker("状態", selection: $status) { ForEach(ARNoteDTO.statusLabels, id: \.0) { Text($0.1).tag($0.0) } }.pickerStyle(.segmented)
                TextField("例: 外壁のひび", text: $text).textFieldStyle(.roundedBorder)
                Button("ここにメモを置く") { Task { await c.place(buildingId: building.id, status: status, text: text); text = "" } }.buttonStyle(.borderedProminent)
            }
            .padding().background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16)).padding()
        }
        .navigationTitle("ARメモ")
        .task { await c.start(buildingId: building.id) }
        .onDisappear { c.arView.session.pause(); c.location.stop() }
    }
}

struct ARNoteContainer: UIViewRepresentable {
    let view: ARView
    func makeUIView(context: Context) -> ARView { view }
    func updateUIView(_ uiView: ARView, context: Context) {}
}
