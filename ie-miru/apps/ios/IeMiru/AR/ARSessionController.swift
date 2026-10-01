import ARKit
import RealityKit
import Combine
import CoreLocation

/// AR セッション管理。Geo Tracking → World Tracking(gravityAndHeading) → AR なし の順でフォールバックする。
/// どの段階で失敗してもアプリ全体は止めず、TrackingMode を下げて続行する。
@MainActor
final class ARSessionController: NSObject, ObservableObject, ARSessionDelegate {
    @Published private(set) var mode: TrackingMode = .mapOnly
    @Published private(set) var notes: [String] = []
    @Published private(set) var cameraPose: CameraPose?
    @Published private(set) var geoTrackingState: String = "-"
    @Published private(set) var lidarAvailable = false

    let arView: ARView
    private(set) var capabilities = ARCapabilities.detect()

    override init() {
        arView = ARView(frame: .zero, cameraMode: .ar, automaticallyConfigureSession: false)
        super.init()
        arView.session.delegate = self
        lidarAvailable = capabilities.lidar
    }

    func start(location: CLLocationCoordinate2D?, hasHeading: Bool) async {
        if capabilities.cameraPermission == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .video)
            capabilities.cameraPermission = AVCaptureDevice.authorizationStatus(for: .video)
        }
        capabilities.geoTrackingAvailableHere = await ARCapabilities.checkGeoAvailability(at: location)
        let (m, n) = capabilities.choose(hasHeading: hasHeading, hasLocation: location != nil)
        mode = m
        notes = n
        run(mode: m)
    }

    private func run(mode: TrackingMode) {
        switch mode {
        case .geo:
            let config = ARGeoTrackingConfiguration()
            config.planeDetection = [.vertical, .horizontal]
            if ARGeoTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { config.frameSemantics.insert(.sceneDepth) }
            arView.session.run(config, options: [.resetTracking, .removeExistingAnchors])
        case .worldHeading:
            arView.session.run(Self.worldConfig(), options: [.resetTracking, .removeExistingAnchors])
        default:
            arView.session.pause()
        }
    }

    static func worldConfig() -> ARWorldTrackingConfiguration {
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravityAndHeading   // -Z = 北, +X = 東, +Y = 上
        config.planeDetection = [.vertical, .horizontal]
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh) { config.sceneReconstruction = .mesh }
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) { config.frameSemantics.insert(.sceneDepth) }
        return config
    }

    func pause() { arView.session.pause() }

    // MARK: ARSessionDelegate

    nonisolated func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let t = frame.camera.transform
        let trackingOK: Bool
        if case .normal = frame.camera.trackingState { trackingOK = true } else { trackingOK = false }
        Task { @MainActor in
            var pose = Self.pose(fromARKit: t)
            if !trackingOK { pose.headingUnreliable = true }
            self.cameraPose = pose
        }
    }

    nonisolated func session(_ session: ARSession, didChange geoTrackingStatus: ARGeoTrackingStatus) {
        let state: String
        switch geoTrackingStatus.state {
        case .localized: state = "localized"
        case .localizing: state = "localizing"
        case .initializing: state = "initializing"
        case .notAvailable: state = "notAvailable"
        @unknown default: state = "unknown"
        }
        Task { @MainActor in
            self.geoTrackingState = state
            if state == "notAvailable" && self.mode == .geo {
                // Geo Tracking 失敗 → World Tracking へフォールバック
                self.mode = .worldHeading
                self.notes.append("Geo Tracking が利用できないため、コンパス精度に切り替えました。")
                self.run(mode: .worldHeading)
            }
        }
    }

    nonisolated func session(_ session: ARSession, didFailWithError error: Error) {
        Task { @MainActor in
            self.notes.append("ARを開始できませんでした（\(error.localizedDescription)）。カメラ＋コンパスで続行します。")
            self.mode = self.capabilities.cameraPermission == .authorized ? .cameraCompass : .compassOnly
        }
    }

    /// domain/ar.ts cameraPoseFromArkitTransform と同じ: ARKit world(E=X, N=-Z, U=Y) へ変換
    nonisolated static func pose(fromARKit t: simd_float4x4) -> CameraPose {
        let r = simd_double3x3(
            rows: [
                SIMD3<Double>(Double(t.columns.0.x), Double(t.columns.1.x), Double(t.columns.2.x)),
                SIMD3<Double>(-Double(t.columns.0.z), -Double(t.columns.1.z), -Double(t.columns.2.z)),
                SIMD3<Double>(Double(t.columns.0.y), Double(t.columns.1.y), Double(t.columns.2.y)),
            ])
        return CameraPose.from(deviceToENU: r, headingAccuracy: 15, source: "arkit")
    }

    /// 画面上の点を raycast して距離 [m] を返す。LiDAR 端末では推定平面/メッシュに当たるため精度が上がる。
    func distance(atScreenPoint p: CGPoint) -> Float? {
        guard let frame = arView.session.currentFrame else { return nil }
        let targets: [ARRaycastQuery.Target] = [.existingPlaneGeometry, .estimatedPlane]
        for target in targets {
            if let q = arView.makeRaycastQuery(from: p, allowing: target, alignment: .any),
               let hit = arView.session.raycast(q).first {
                let cam = frame.camera.transform.columns.3
                let pos = hit.worldTransform.columns.3
                return simd_distance(SIMD3(cam.x, cam.y, cam.z), SIMD3(pos.x, pos.y, pos.z))
            }
        }
        return nil
    }

    /// ENU オフセット[m]（東, 北, 上）を AR 世界座標へ。gravityAndHeading 前提で原点はセッション開始位置ではなくカメラ位置を基準にする。
    func worldPosition(enuFromCamera e: SIMD3<Float>) -> SIMD3<Float>? {
        guard let cam = arView.session.currentFrame?.camera.transform.columns.3 else { return nil }
        return SIMD3(cam.x + e.x, cam.y + e.z, cam.z - e.y)
    }
}
