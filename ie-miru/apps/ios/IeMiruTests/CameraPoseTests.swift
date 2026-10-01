import XCTest
import simd
@testable import IeMiru

/// TS 側 packages/domain/test/heading.test.ts と同じ期待値。
final class CameraPoseTests: XCTestCase {
    func testIdentityLooksDown() {
        let p = CameraPose.from(coreMotionQuaternion: simd_quatd(ix: 0, iy: 0, iz: 0, r: 1), headingAccuracy: nil)
        XCTAssertEqual(p.pitch, -90, accuracy: 1e-6)
        XCTAssertTrue(p.headingUnreliable)
    }

    func testRotatedAboutXLooksWest() {
        let s = 0.5.squareRoot()
        let p = CameraPose.from(coreMotionQuaternion: simd_quatd(ix: s, iy: 0, iz: 0, r: s), headingAccuracy: 5)
        XCTAssertEqual(p.pitch, 0, accuracy: 1e-6)
        XCTAssertEqual(p.heading, 270, accuracy: 1e-6)
    }
}
