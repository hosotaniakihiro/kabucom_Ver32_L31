import Foundation
import simd

/// packages/domain/src/heading.ts の CameraPose と同じ形・同じ計算。
struct CameraPose: Codable, Equatable {
    var heading: Double      // 背面カメラ光軸の方位 [deg, 真北, 0..360)
    var yaw: Double          // heading を (-180, 180]
    var pitch: Double        // 仰角 [deg]
    var roll: Double         // 光軸まわりの傾き [deg]
    var headingAccuracy: Double?
    var headingUnreliable: Bool
    var source: String       // arkit | coremotion | device_orientation | compass_only | manual

    static let pitchUnreliableDeg = 75.0
    static let headingUnreliableDeg = 45.0

    /// 端末→ENU(x=東, y=北, z=上) 回転行列から姿勢を作る。背面カメラは端末 -Z。
    static func from(deviceToENU r: simd_double3x3, headingAccuracy: Double?, source: String) -> CameraPose {
        let f = simd_normalize(r * SIMD3<Double>(0, 0, -1))
        let up = simd_normalize(r * SIMD3<Double>(0, 1, 0))
        var heading = atan2(f.x, f.y) * 180 / .pi
        heading = (heading.truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360)
        let pitch = asin(max(-1, min(1, f.z))) * 180 / .pi
        var roll = 0.0
        if hypot(f.x, f.y) > 1e-6 {
            let right = simd_normalize(simd_cross(f, SIMD3<Double>(0, 0, 1)))
            let upRef = simd_cross(right, f)
            roll = atan2(simd_dot(up, right), simd_dot(up, upRef)) * 180 / .pi
        }
        let unreliable = abs(pitch) > pitchUnreliableDeg || (headingAccuracy.map { $0 > headingUnreliableDeg } ?? false)
        return CameraPose(heading: heading, yaw: heading > 180 ? heading - 360 : heading, pitch: pitch, roll: roll,
                          headingAccuracy: headingAccuracy, headingUnreliable: unreliable, source: source)
    }

    /// CoreMotion xTrueNorthZVertical の四元数（端末→NWU）から。
    static func from(coreMotionQuaternion q: simd_quatd, headingAccuracy: Double?) -> CameraPose {
        let m = simd_double3x3(q)  // columns
        // NWU → ENU: E = -W, N = N, U = U （行の入れ替え）
        let rows = [m.transpose.columns.0, m.transpose.columns.1, m.transpose.columns.2] // rows of m
        let enu = simd_double3x3(rows: [-rows[1], rows[0], rows[2]])
        return from(deviceToENU: enu, headingAccuracy: headingAccuracy, source: "coremotion")
    }
}
