import SwiftUI

@main
struct IeMiruApp: App {
    var body: some Scene {
        WindowGroup { RootView() }
    }
}

/// トップは「見る」「マップ」「保存した家」の3つだけ
struct RootView: View {
    @State private var tab = 0
    var body: some View {
        TabView(selection: $tab) {
            LookView(onShowMap: { tab = 1 }).tabItem { Label("見る", systemImage: "camera.viewfinder") }.tag(0)
            MapPickerView().tabItem { Label("マップ", systemImage: "map") }.tag(1)
            SavedListView().tabItem { Label("保存した家", systemImage: "house") }.tag(2)
        }
    }
}
