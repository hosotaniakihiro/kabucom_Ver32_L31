import SwiftUI

/// 建物詳細（下からのシート）。「この家について」「この家でできること」。
struct BuildingDetailView: View {
    let buildingId: String
    @State private var report: BuildingReportDTO?
    @State private var offline = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Group {
                if let r = report { content(r) } else if let error { Text(error) } else { ProgressView("情報を集めています…") }
            }
            .navigationTitle(report.map { title($0) } ?? "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { if let r = report { SaveButton(building: r.building) } }
        }
        .presentationDetents([.medium, .large])
        .task { await load() }
    }

    private func title(_ r: BuildingReportDTO) -> String {
        [r.facts.usage.value ?? "建物", r.facts.structure.value].compactMap { $0 }.joined(separator: "・")
    }

    private func load() async {
        do { (report, offline) = try await APIClient.shared.report(buildingId: buildingId) } catch { self.error = "情報を取得できませんでした。" }
    }

    @ViewBuilder private func content(_ r: BuildingReportDTO) -> some View {
        List {
            if offline { Text("オフライン表示（前回取得の情報）").foregroundStyle(.orange) }
            if r.dataMode != "live" { Text("デモ／モックデータを含みます（実データではありません）").font(.caption).foregroundStyle(.brown) }
            Section("この家について") {
                DisclosureGroup("参考相場") {
                    if let v = r.valuation { SourcedRow(label: "AI参考査定", s: v) { "\(Yen.range($0.estimatedLow, $0.estimatedHigh))" } }
                    if let c = r.comparables { SourcedRow(label: "周辺取引事例", s: c) { "\($0.count)件" } }
                    Text("※この建物そのものの売買価格ではありません。正式な不動産鑑定ではありません。").font(.caption2)
                }
                DisclosureGroup("土地") { SourcedRow(label: "最寄りの地価", s: r.landPrices) { $0.first.map { "\(Yen.unit($0.pricePerM2))（\($0.kind) \($0.year)年）" } ?? "-" } }
                DisclosureGroup("用途地域") {
                    SourcedRow(label: "用途地域", s: r.zoning) { $0.useDistrict }
                    SourcedRow(label: "建ぺい率", s: r.zoning) { $0.coverageRatioPct.map { "\(Int($0))%" } ?? "データなし" }
                    SourcedRow(label: "容積率", s: r.zoning) { $0.floorAreaRatioPct.map { "\(Int($0))%" } ?? "データなし" }
                }
                DisclosureGroup("災害: \(r.hazardSummary.headline)") {
                    ForEach(r.hazards, id: \.type) { h in
                        HStack { Text(h.label).frame(width: 70, alignment: .leading); Text(h.statusLabel).foregroundStyle(h.status == "in_zone" ? .red : .secondary) }
                    }
                }
                DisclosureGroup("建物概要") {
                    SourcedRow(label: "推定築年", s: r.facts.builtYear) { "\($0)年" }
                    SourcedRow(label: "階数", s: r.facts.floorsAbove) { "地上\($0)階" }
                    SourcedRow(label: "高さ", s: r.facts.heightM) { String(format: "%.1fm", $0) }
                }
            }
            Section("この家でできること") {
                NavigationLink("買う") { BuySimulationView(report: r) }
                NavigationLink("売る") { SellSimulationView(report: r) }
                NavigationLink("直す") { FixView(building: r.building) }
                NavigationLink("貸す") { RentSimulationView(report: r) }
                NavigationLink("建て替える") { RebuildARView(report: r) }
            }
            Section { ForEach(r.disclaimers, id: \.self) { Text($0).font(.caption2) } }
        }
    }
}

struct SourcedRow<T: Codable>: View {
    let label: String
    let s: Sourced<T>
    let format: (T) -> String
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(s.value.map(format) ?? s.statusLabel).foregroundStyle(s.value == nil ? .secondary : .primary)
            Spacer()
            Text(s.kindLabel).font(.caption2).padding(3).background(Color.blue.opacity(0.15), in: RoundedRectangle(cornerRadius: 4))
            if s.isMock { Text("モック").font(.caption2).padding(3).background(Color.yellow.opacity(0.3), in: RoundedRectangle(cornerRadius: 4)) }
        }
    }
}

enum Yen {
    static func man(_ yen: Double) -> String {
        let man = Int((yen / 10_000).rounded())
        if man >= 10_000 { let oku = man / 10_000; let rest = man % 10_000; return rest == 0 ? "\(oku)億円" : "\(oku)億\(rest.formatted())万円" }
        return "\(man.formatted())万円"
    }
    static func range(_ lo: Double, _ hi: Double) -> String { "\(man(lo).replacingOccurrences(of: "万円", with: ""))〜\(man(hi))" }
    static func unit(_ perM2: Double) -> String { String(format: "%.1f万円/㎡", perM2 / 10_000) }
}
