import ARKit
import AVFoundation
import CoreLocation

/// domain/ar.ts の ArCapabilities / chooseTrackingMode と同じ判定。
enum TrackingMode: String { case geo, worldHeading = "world_heading", cameraCompass = "camera_compass", compassOnly = "compass_only", mapOnly = "map_only" }

struct ARCapabilities {
    var worldTracking: Bool
    var geoTrackingDevice: Bool
    var geoTrackingAvailableHere: Bool?
    var lidar: Bool
    var cameraPermission: AVAuthorizationStatus

    static func detect() -> ARCapabilities {
        ARCapabilities(
            worldTracking: ARWorldTrackingConfiguration.isSupported,
            geoTrackingDevice: ARGeoTrackingConfiguration.isSupported,
            geoTrackingAvailableHere: nil,
            lidar: ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh),
            cameraPermission: AVCaptureDevice.authorizationStatus(for: .video)
        )
    }

    /// 現在地で Geo Tracking が使えるか（Apple の対応都市のみ）。失敗しても false として続行する。
    static func checkGeoAvailability(at coordinate: CLLocationCoordinate2D?) async -> Bool {
        guard ARGeoTrackingConfiguration.isSupported else { return false }
        return await withCheckedContinuation { cont in
            if let c = coordinate {
                ARGeoTrackingConfiguration.checkAvailability(at: c) { ok, _ in cont.resume(returning: ok) }
            } else {
                ARGeoTrackingConfiguration.checkAvailability { ok, _ in cont.resume(returning: ok) }
            }
        }
    }

    func choose(hasHeading: Bool, hasLocation: Bool) -> (TrackingMode, [String]) {
        var notes: [String] = []
        let camera = cameraPermission == .authorized
        if !camera { notes.append("カメラが使えないため、方位と地図で建物を選びます。") }
        if !hasLocation { notes.append("現在地が使えないため、地図から建物を選んでください。"); return (.mapOnly, notes) }
        if camera && worldTracking && geoTrackingDevice && geoTrackingAvailableHere == true { return (.geo, notes) }
        if camera && worldTracking {
            if geoTrackingDevice && geoTrackingAvailableHere == false { notes.append("この地域は Geo Tracking 対象外のため、コンパス精度で特定します。") }
            return (.worldHeading, notes)
        }
        if !hasHeading { notes.append("方位が取得できないため、地図から建物を選んでください。"); return (.mapOnly, notes) }
        return (camera ? .cameraCompass : .compassOnly, notes)
    }
}
