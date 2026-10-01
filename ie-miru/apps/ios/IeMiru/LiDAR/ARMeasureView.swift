import SwiftUI
import ARKit
import RealityKit

/// LiDAR（sceneReconstruction）で修理箇所の長さ・面積を測る。非 LiDAR 端末では表示しない（基本機能は影響なし）。
/// domain/lidar.ts の polylineLength / polygonArea3 と同じ計算。
struct ARMeasureView: View {
    static var isSupported: Bool { ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) }
    var onMeasured: (Double) -> Void
    @StateObject private var m = MeasureController()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack(alignment: .bottom) {
            ARNoteContainer(view: m.arView).ignoresSafeArea()
                .onTapGesture { p in m.addPoint(at: p) }
            VStack(spacing: 8) {
                Text(m.points.isEmpty ? "端点をタップしてください（LiDAR）" : m.summary).font(.headline)
                HStack {
                    Button("リセット") { m.reset() }
                    Button("この長さを使う") { onMeasured(m.length); dismiss() }.disabled(m.points.count < 2).buttonStyle(.borderedProminent)
                }
            }.padding().background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16)).padding()
        }
        .navigationTitle("サイズを測る")
        .onAppear { m.start() }
        .onDisappear { m.arView.session.pause() }
    }
}

@MainActor
final class MeasureController: ObservableObject {
    let arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
    @Published var points: [SIMD3<Float>] = []

    func start() {
        let c = ARWorldTrackingConfiguration()
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) { c.sceneReconstruction = .mesh }
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { c.frameSemantics.insert(.sceneDepth) }
        c.planeDetection = [.vertical, .horizontal]
        arView.session.run(c)
    }

    func addPoint(at p: CGPoint) {
        guard let q = arView.makeRaycastQuery(from: p, allowing: .estimatedPlane, alignment: .any), let hit = arView.session.raycast(q).first else { return }
        let v = hit.worldTransform.columns.3
        let pos = SIMD3(v.x, v.y, v.z)
        points.append(pos)
        let dot = ModelEntity(mesh: .generateSphere(radius: 0.008), materials: [SimpleMaterial(color: .yellow, isMetallic: false)])
        let a = AnchorEntity(world: pos)
        a.addChild(dot)
        arView.scene.addAnchor(a)
    }

    func reset() {
        points = []
        arView.scene.anchors.removeAll()
    }

    var length: Double {
        zip(points, points.dropFirst()).reduce(0) { $0 + Double(simd_distance($1.0, $1.1)) }
    }

    /// Newell 法
    var area: Double {
        guard points.count >= 3 else { return 0 }
        var n = SIMD3<Float>(0, 0, 0)
        for i in 0..<points.count {
            let a = points[i], b = points[(i + 1) % points.count]
            n.x += (a.y - b.y) * (a.z + b.z); n.y += (a.z - b.z) * (a.x + b.x); n.z += (a.x - b.x) * (a.y + b.y)
        }
        return Double(simd_length(n)) / 2
    }

    var summary: String {
        var s = length < 1 ? "長さ 約\(Int(length * 100))cm" : String(format: "長さ 約%.2fm", length)
        if points.count >= 3 { s += String(format: " ・ 面積 約%.2f㎡", area) }
        return s
    }
}
