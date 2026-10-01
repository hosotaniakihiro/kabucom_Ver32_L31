import Foundation
import CoreLocation

/// packages/domain/src/geo.ts と同じ計算（ARメモの GPS 推定配置に使う）
enum GeoMath {
    static let R = 6_371_008.8
    static func distance(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> Double {
        let dLat = (b.latitude - a.latitude) * .pi / 180, dLng = (b.longitude - a.longitude) * .pi / 180
        let s = pow(sin(dLat / 2), 2) + cos(a.latitude * .pi / 180) * cos(b.latitude * .pi / 180) * pow(sin(dLng / 2), 2)
        return 2 * R * asin(min(1, sqrt(s)))
    }
    static func bearing(_ a: CLLocationCoordinate2D, _ b: CLLocationCoordinate2D) -> Double {
        let φ1 = a.latitude * .pi / 180, φ2 = b.latitude * .pi / 180, Δλ = (b.longitude - a.longitude) * .pi / 180
        let y = sin(Δλ) * cos(φ2), x = cos(φ1) * sin(φ2) - sin(φ1) * cos(φ2) * cos(Δλ)
        return (atan2(y, x) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }
    static func destination(_ from: CLLocationCoordinate2D, bearing: Double, distance: Double) -> CLLocationCoordinate2D {
        let δ = distance / R, θ = bearing * .pi / 180, φ1 = from.latitude * .pi / 180, λ1 = from.longitude * .pi / 180
        let φ2 = asin(sin(φ1) * cos(δ) + cos(φ1) * sin(δ) * cos(θ))
        let λ2 = λ1 + atan2(sin(θ) * sin(δ) * cos(φ1), cos(δ) - sin(φ1) * sin(φ2))
        return CLLocationCoordinate2D(latitude: φ2 * 180 / .pi, longitude: λ2 * 180 / .pi)
    }
}
