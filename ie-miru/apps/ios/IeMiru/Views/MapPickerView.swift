import SwiftUI
import MapKit

/// 地図から建物を手動選択（位置情報不許可・精度不良時の受け皿）
struct MapPickerView: View {
    @State private var position: MapCameraPosition = .region(MKCoordinateRegion(center: CLLocationCoordinate2D(latitude: 35.681236, longitude: 139.767125), latitudinalMeters: 300, longitudinalMeters: 300))
    @State private var buildings: [BuildingDTO] = []
    @State private var selected: BuildingDTO?
    @State private var status = "建物をタップして選んでください。"

    var body: some View {
        NavigationStack {
            MapReader { proxy in
                Map(position: $position) {
                    UserAnnotation()
                    ForEach(buildings) { b in
                        MapPolygon(coordinates: b.footprint.map { CLLocationCoordinate2D(latitude: $0.lat, longitude: $0.lng) })
                            .foregroundStyle(.teal.opacity(0.25)).stroke(.teal, lineWidth: 1)
                    }
                }
                .onMapCameraChange(frequency: .onEnd) { ctx in Task { await load(ctx.region.center) } }
                .onTapGesture { pt in
                    guard let c = proxy.convert(pt, from: .local) else { return }
                    selected = buildings.first { contains($0.footprint, c) }
                }
            }
            .safeAreaInset(edge: .bottom) { Text(status).font(.footnote).padding(8).frame(maxWidth: .infinity).background(.regularMaterial) }
            .navigationTitle("マップ")
            .sheet(item: $selected) { b in BuildingDetailView(buildingId: b.id) }
        }
    }

    private func load(_ c: CLLocationCoordinate2D) async {
        do {
            buildings = try await APIClient.shared.nearby(lat: c.latitude, lng: c.longitude, radius: 150)
            status = buildings.isEmpty ? "この付近の建物データがありません。" : "建物をタップして選んでください（\(buildings.count)件）" + (buildings.first?.source.mode == "demo" ? " ※デモ建物" : "")
        } catch { status = "通信できませんでした。" }
    }

    private func contains(_ ring: [LatLngDTO], _ p: CLLocationCoordinate2D) -> Bool {
        var inside = false
        var j = ring.count - 1
        for i in 0..<ring.count {
            let a = ring[i], b = ring[j]
            if (a.lat > p.latitude) != (b.lat > p.latitude) && p.longitude < (b.lng - a.lng) * (p.latitude - a.lat) / (b.lat - a.lat) + a.lng { inside.toggle() }
            j = i
        }
        return inside
    }
}
