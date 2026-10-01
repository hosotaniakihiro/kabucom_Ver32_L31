import CoreMotion
import Combine
import simd

/// CoreMotion で端末姿勢を取り、背面カメラの heading / pitch / yaw / roll を公開する。
/// 端末方位（CLHeading）＝端末上端の方位であり、縦持ちで建物へ向けたときのカメラ方位とは異なるため、
/// 建物特定には必ずこちら（または ARKit のカメラ姿勢）を使う。
@MainActor
final class MotionService: ObservableObject {
    @Published private(set) var pose: CameraPose?
    @Published private(set) var available: Bool = CMMotionManager().isDeviceMotionAvailable

    private let manager = CMMotionManager()
    private var smoothedHeading: Double?

    /// headingAccuracy は CLHeading から供給する（CoreMotion 単体では得られない）
    var headingAccuracyProvider: () -> Double? = { nil }

    func start() {
        guard manager.isDeviceMotionAvailable else { available = false; return }
        let frames = CMMotionManager.availableAttitudeReferenceFrames()
        guard frames.contains(.xTrueNorthZVertical) else { available = false; return }
        manager.deviceMotionUpdateInterval = 1.0 / 30.0
        manager.startDeviceMotionUpdates(using: .xTrueNorthZVertical, to: .main) { [weak self] motion, _ in
            guard let self, let motion else { return }
            let q = motion.attitude.quaternion
            var p = CameraPose.from(coreMotionQuaternion: simd_quatd(ix: q.x, iy: q.y, iz: q.z, r: q.w),
                                    headingAccuracy: self.headingAccuracyProvider())
            p.heading = self.smooth(p.heading)
            p.yaw = p.heading > 180 ? p.heading - 360 : p.heading
            self.pose = p
        }
    }

    func stop() { manager.stopDeviceMotionUpdates() }

    /// 円周 EMA（domain/heading.ts の smoothHeading と同じ）
    private func smooth(_ next: Double, factor: Double = 0.25) -> Double {
        guard let prev = smoothedHeading else { smoothedHeading = next; return next }
        var d = next - prev
        if d > 180 { d -= 360 }
        if d < -180 { d += 360 }
        let v = ((prev + d * factor).truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360)
        smoothedHeading = v
        return v
    }
}
