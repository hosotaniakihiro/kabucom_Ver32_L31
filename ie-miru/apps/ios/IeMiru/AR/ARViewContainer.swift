import SwiftUI
import RealityKit

/// ARView を SwiftUI に載せる。AR 非対応時は呼ばれない（LookView 側でカメラ/コンパス表示に切り替える）。
struct ARViewContainer: UIViewRepresentable {
    let controller: ARSessionController
    func makeUIView(context: Context) -> ARView { controller.arView }
    func updateUIView(_ uiView: ARView, context: Context) {}
}
