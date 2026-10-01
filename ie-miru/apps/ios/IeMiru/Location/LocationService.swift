import CoreLocation
import Combine

/// API に送る位置情報。packages/domain/src/location.ts の LocationFix と同じ形。
struct LocationFix: Codable, Equatable {
    var latitude: Double
    var longitude: Double
    var horizontalAccuracy: Double
    var altitude: Double?
    var heading: Double?
    var headingAccuracy: Double?
    var timestamp: Double   // epoch ms
}

enum LocationPermission: String {
    case granted, denied, restricted, notDetermined = "not_determined", unsupported
}

/// 精度判定（domain/location.ts の classifyAccuracy と同じ閾値）
enum AccuracyLevel { case good, fair, poor, invalid
    static func of(_ acc: Double) -> AccuracyLevel {
        if !acc.isFinite || acc < 0 { return .invalid }
        if acc <= 15 { return .good }
        if acc <= 35 { return .fair }
        return .poor
    }
}

/// CoreLocation ラッパー。許可拒否・取得失敗でもクラッシュせず、状態と案内文を公開する。
@MainActor
final class LocationService: NSObject, ObservableObject {
    @Published private(set) var permission: LocationPermission = .notDetermined
    @Published private(set) var fix: LocationFix?
    @Published private(set) var fallbackMessage: String?

    private let manager = CLLocationManager()
    private var lastLocation: CLLocation?
    private var lastHeading: CLHeading?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyBest
        manager.distanceFilter = 1
        manager.headingFilter = 1
        manager.headingOrientation = .portrait
        updatePermission(manager.authorizationStatus)
    }

    func start() {
        guard CLLocationManager.locationServicesEnabled() else {
            permission = .unsupported
            fallbackMessage = "この端末では位置情報を利用できません。地図から建物を手動で選んでください。"
            return
        }
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedWhenInUse, .authorizedAlways:
            manager.startUpdatingLocation()
            if CLLocationManager.headingAvailable() { manager.startUpdatingHeading() }
        default:
            break
        }
    }

    func stop() {
        manager.stopUpdatingLocation()
        manager.stopUpdatingHeading()
    }

    private func updatePermission(_ status: CLAuthorizationStatus) {
        switch status {
        case .authorizedAlways, .authorizedWhenInUse:
            permission = .granted
            fallbackMessage = nil
        case .denied:
            permission = .denied
            fallbackMessage = "位置情報が許可されていません。地図から建物を手動で選んでください。"
        case .restricted:
            permission = .restricted
            fallbackMessage = "位置情報が制限されています。地図から建物を手動で選んでください。"
        case .notDetermined:
            permission = .notDetermined
        @unknown default:
            permission = .unsupported
        }
    }

    private func publish() {
        guard let loc = lastLocation else { return }
        var heading: Double?
        var headingAccuracy: Double?
        if let h = lastHeading, h.headingAccuracy >= 0 {
            // trueHeading が負なら真北が取れていない → 磁北を偏角補正せず null 扱いにしない（精度を大きく）
            if h.trueHeading >= 0 {
                heading = h.trueHeading
                headingAccuracy = h.headingAccuracy
            } else {
                heading = h.magneticHeading
                headingAccuracy = max(h.headingAccuracy, 20)
            }
        }
        fix = LocationFix(
            latitude: loc.coordinate.latitude,
            longitude: loc.coordinate.longitude,
            horizontalAccuracy: loc.horizontalAccuracy,
            altitude: loc.verticalAccuracy >= 0 ? loc.altitude : nil,
            heading: heading,
            headingAccuracy: headingAccuracy,
            timestamp: loc.timestamp.timeIntervalSince1970 * 1000
        )
        switch AccuracyLevel.of(loc.horizontalAccuracy) {
        case .poor, .invalid:
            fallbackMessage = "現在地の精度が低いため、建物を手動選択してください。"
        case .fair:
            fallbackMessage = "現在地の精度がやや低いため、候補から正しい建物を選んでください。"
        case .good:
            fallbackMessage = nil
        }
    }
}

extension LocationService: CLLocationManagerDelegate {
    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            self.updatePermission(status)
            if self.permission == .granted { self.start() }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let last = locations.last else { return }
        Task { @MainActor in
            self.lastLocation = last
            self.publish()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateHeading newHeading: CLHeading) {
        Task { @MainActor in
            self.lastHeading = newHeading
            self.publish()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in
            // kCLErrorLocationUnknown は一時的。その他は手動選択へ誘導（クラッシュさせない）
            if (error as? CLError)?.code != .locationUnknown {
                self.fallbackMessage = "現在地を取得できませんでした。地図から建物を手動で選んでください。"
            }
        }
    }

    nonisolated func locationManagerShouldDisplayHeadingCalibration(_ manager: CLLocationManager) -> Bool { true }
}
