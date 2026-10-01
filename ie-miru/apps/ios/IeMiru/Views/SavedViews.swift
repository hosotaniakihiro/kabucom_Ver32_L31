import SwiftUI

struct SavedBuildingDTO: Codable, Identifiable, Hashable {
    struct Snapshot: Codable, Hashable { var lat: Double; var lng: Double; var usage: String?; var sourceMode: String }
    var buildingId: String
    var nickname: String
    var status: String
    var snapshot: Snapshot?
    var updatedAt: String
    var id: String { buildingId }

    static let statuses: [(String, String)] = [("interested", "気になる"), ("own", "自宅（所有）"), ("family_home", "実家"), ("selling", "売却検討中"), ("renovating", "リフォーム中"), ("watching", "ウォッチ中")]
}

enum SavedStore {
    static func list() async -> (items: [SavedBuildingDTO], offline: Bool) {
        struct R: Codable { var saved: [SavedBuildingDTO] }
        if let d = try? await APIClient.shared.get("v1/saved"), let r = try? JSONDecoder().decode(R.self, from: d) {
            UserDefaults.standard.set(d, forKey: "iemiru.saved")
            return (r.saved, false)
        }
        if let d = UserDefaults.standard.data(forKey: "iemiru.saved"), let r = try? JSONDecoder().decode(R.self, from: d) { return (r.saved, true) }
        return ([], true)
    }
}

/// 詳細画面の「保存」
struct SaveButton: View {
    let building: BuildingDTO
    @State private var show = false
    @State private var nickname = ""
    @State private var status = "interested"
    @State private var saved = false

    var body: some View {
        Button(saved ? "★ 保存済み" : "☆ 保存") { show = true }
            .task { if let cur = await SavedStore.list().items.first(where: { $0.buildingId == building.id }) { saved = true; nickname = cur.nickname; status = cur.status } }
            .sheet(isPresented: $show) {
                NavigationStack {
                    Form {
                        TextField("呼び名（例: 駅前の青い家）", text: $nickname)
                        Picker("状態", selection: $status) { ForEach(SavedBuildingDTO.statuses, id: \.0) { Text($0.1).tag($0.0) } }
                    }
                    .navigationTitle("この家を保存")
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) { Button("保存") { Task { await save() } } }
                        ToolbarItem(placement: .cancellationAction) { Button("キャンセル") { show = false } }
                    }
                }.presentationDetents([.medium])
            }
    }

    private func save() async {
        struct Snap: Encodable { var lat: Double; var lng: Double; var usage: String?; var sourceMode: String }
        struct Body: Encodable { var buildingId: String; var nickname: String; var status: String; var snapshot: Snap }
        let body = Body(buildingId: building.id, nickname: nickname, status: status, snapshot: Snap(lat: building.centroid.lat, lng: building.centroid.lng, usage: building.usage, sourceMode: building.source.mode))
        if (try? await APIClient.shared.send("v1/saved", method: "POST", json: body)) != nil { saved = true }
        show = false
    }
}

struct SavedListView: View {
    @State private var items: [SavedBuildingDTO] = []
    @State private var offline = false
    @State private var selected: SavedBuildingDTO?

    var body: some View {
        NavigationStack {
            List {
                if offline { Text("オフラインのため、前回読み込んだ一覧を表示しています。").font(.caption) }
                if items.isEmpty { Text("まだ保存した家はありません。").foregroundStyle(.secondary) }
                ForEach(items) { s in
                    Button { selected = s } label: {
                        VStack(alignment: .leading) {
                            Text(s.nickname).bold()
                            Text([SavedBuildingDTO.statuses.first { $0.0 == s.status }?.1, s.snapshot?.usage, s.snapshot?.sourceMode == "demo" ? "デモ建物" : nil].compactMap { $0 }.joined(separator: "・")).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { idx in
                    for i in idx { let id = items[i].buildingId; Task { _ = try? await APIClient.shared.send("v1/saved/\(id.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? id)", method: "DELETE", json: Optional<String>.none) } }
                    items.remove(atOffsets: idx)
                }
            }
            .navigationTitle("保存した家")
            .refreshable { await load() }
            .task { await load() }
            .sheet(item: $selected) { s in BuildingDetailView(buildingId: s.buildingId) }
        }
    }

    private func load() async { (items, offline) = await SavedStore.list() }
}
