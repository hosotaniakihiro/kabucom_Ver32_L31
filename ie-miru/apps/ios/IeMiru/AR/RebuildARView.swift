import SwiftUI
import ARKit
import RealityKit

struct RebuildPlanDTO: Codable {
    struct Check: Codable, Hashable { var key: String; var label: String; var status: String; var detail: String }
    var preset: String; var label: String; var floors: Int; var footprintM2: Double; var totalFloorM2: Double
    var heightM: Double; var widthM: Double; var depthM: Double; var roof: String; var checks: [Check]; var notes: [String]
}

/// 「建て替える」AR。検出した地面をタップすると、簡易ボリューム（箱＋屋根）を実寸で置く。
/// 既存 3D 資産（Madori3D / sumai3d）が見つかれば USDZ 読み込みに差し替える（docs/IE_MIRU_AR.md）。
struct RebuildARView: View {
    let report: BuildingReportDTO
    @State private var preset = "two_story"
    @State private var plan: RebuildPlanDTO?
    @StateObject private var holder = RebuildARHolder()

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .top) {
                ARNoteContainer(view: holder.arView).ignoresSafeArea(edges: .top)
                    .onTapGesture { p in if let plan { holder.place(plan: plan, at: p) } }
                Text("地面をタップして配置（参考表示）").font(.caption).padding(6).background(.ultraThinMaterial, in: Capsule()).padding(.top, 8)
            }
            List {
                Picker("プラン", selection: $preset) {
                    Text("2階建て").tag("two_story"); Text("3階建て").tag("three_story"); Text("賃貸併用").tag("rental_combo")
                }.pickerStyle(.segmented)
                if let p = plan {
                    Text(String(format: "建築面積 約%.0f㎡ ・ 延床 約%.0f㎡ ・ 高さ 約%.1fm", p.footprintM2, p.totalFloorM2, p.heightM))
                    Section("参考: 用途地域・建ぺい率・容積率") {
                        ForEach(p.checks, id: \.self) { c in
                            VStack(alignment: .leading) {
                                Text("\(c.label): \(["within": "範囲内（参考）", "exceeds": "超過の可能性", "unknown": "要確認"][c.status] ?? c.status)")
                                Text(c.detail).font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    }
                    ForEach(p.notes, id: \.self) { Text($0).font(.caption2) }
                }
            }.frame(maxHeight: 320)
        }
        .navigationTitle("建て替える")
        .task { holder.start(); await load() }
        .onChange(of: preset) { _, _ in Task { await load() } }
        .onDisappear { holder.arView.session.pause() }
    }

    private func load() async {
        struct Body: Encodable { var preset: String; var landAreaM2: Double?; var coverageRatioPct: Double?; var floorAreaRatioPct: Double?; var useDistrict: String?; var existingFootprintM2: Double }
        struct R: Decodable { var plan: RebuildPlanDTO }
        let z = report.zoning.value
        let body = Body(preset: preset, landAreaM2: nil, coverageRatioPct: z?.coverageRatioPct, floorAreaRatioPct: z?.floorAreaRatioPct, useDistrict: z?.useDistrict, existingFootprintM2: report.building.footprintAreaM2)
        if let d = try? await APIClient.shared.send("v1/simulations/rebuild", method: "POST", json: body) { plan = try? JSONDecoder().decode(R.self, from: d).plan }
    }
}

@MainActor
final class RebuildARHolder: ObservableObject {
    let arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
    private var anchor: AnchorEntity?

    func start() {
        let c = ARWorldTrackingConfiguration()
        c.planeDetection = [.horizontal]
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) { c.sceneReconstruction = .mesh }
        arView.session.run(c)
    }

    func place(plan: RebuildPlanDTO, at p: CGPoint) {
        guard let q = arView.makeRaycastQuery(from: p, allowing: .estimatedPlane, alignment: .horizontal), let hit = arView.session.raycast(q).first else { return }
        if let anchor { arView.scene.removeAnchor(anchor) }
        let a = AnchorEntity(world: hit.worldTransform)
        let roofH: Float = plan.roof == "gable" ? 1.8 : 0.6
        let floorH = (Float(plan.heightM) - roofH) / Float(plan.floors)
        for i in 0..<plan.floors {
            let color: UIColor = plan.preset == "rental_combo" && i == 0 ? UIColor.systemIndigo.withAlphaComponent(0.6) : UIColor.white.withAlphaComponent(0.75)
            let box = ModelEntity(mesh: .generateBox(width: Float(plan.widthM), height: floorH * 0.98, depth: Float(plan.depthM)), materials: [SimpleMaterial(color: color, isMetallic: false)])
            box.position.y = floorH * Float(i) + floorH / 2
            a.addChild(box)
        }
        let roof = ModelEntity(mesh: .generateBox(width: Float(plan.widthM) + 0.3, height: roofH, depth: Float(plan.depthM) + 0.3), materials: [SimpleMaterial(color: .darkGray, isMetallic: false)])
        roof.position.y = floorH * Float(plan.floors) + roofH / 2
        a.addChild(roof)
        arView.scene.addAnchor(a)
        anchor = a
    }
}
