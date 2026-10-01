import SwiftUI

/// 試算は API（/v1/simulations/*）で domain と同じ計算を行う。全数値は編集可能。
struct CostLineDTO: Codable, Hashable { var key: String; var label: String; var amount: Double; var estimated: Bool; var note: String? }

struct BuySimulationView: View {
    let report: BuildingReportDTO
    @State private var price: Double = 0
    @State private var renovation: Double = 0
    @State private var loan: Double = 0
    @State private var rate: Double = 1.0
    @State private var years: Double = 35
    @State private var overrides: [String: Double] = [:]
    @State private var result: BuyResultDTO?

    struct BuyResultDTO: Codable { var costs: [CostLineDTO]; var totalCosts: Double; var totalAcquisition: Double; var monthlyPayment: Double; var notes: [String] }
    struct Resp: Codable { var result: BuyResultDTO }

    var body: some View {
        Form {
            if let v = report.valuation?.value { Text("AI参考査定 \(Yen.range(v.estimatedLow, v.estimatedHigh)) の中央値を初期値にしています").font(.caption) }
            Section("購入条件") {
                ManField(label: "想定購入価格", yen: $price)
                ManField(label: "リフォーム予算", yen: $renovation)
                ManField(label: "借入額", yen: $loan)
                Stepper("金利 \(rate, specifier: "%.2f")%", value: $rate, in: 0...10, step: 0.05)
                Stepper("返済期間 \(Int(years))年", value: $years, in: 1...50)
            }
            if let r = result {
                Section("諸費用（編集できます）") {
                    ForEach(r.costs, id: \.key) { c in
                        ManField(label: c.label + (c.estimated ? "（概算）" : ""), yen: Binding(get: { overrides[c.key] ?? c.amount }, set: { overrides[c.key] = $0 }))
                    }
                }
                Section("結果") {
                    LabeledContent("諸費用 合計", value: Yen.man(r.totalCosts))
                    LabeledContent("総取得費", value: Yen.man(r.totalAcquisition)).bold()
                    LabeledContent("月々の返済", value: Yen.man(r.monthlyPayment))
                    ForEach(r.notes, id: \.self) { Text($0).font(.caption2) }
                }
            }
        }
        .navigationTitle("買う")
        .task {
            let mid = report.valuation?.value?.estimatedMid ?? 0
            price = mid; loan = (mid * 0.9).rounded()
            await recalc()
        }
        .onChange(of: [price, renovation, loan, rate, years]) { _, _ in Task { await recalc() } }
        .onChange(of: overrides) { _, _ in Task { await recalc() } }
    }

    private func recalc() async {
        struct Body: Encodable { var purchasePrice: Double; var renovationBudget: Double; var loanAmount: Double; var interestRatePct: Double; var loanYears: Double; var overrides: [String: Double] }
        guard let data = try? await APIClient.shared.send("v1/simulations/buy", method: "POST", json: Body(purchasePrice: price, renovationBudget: renovation, loanAmount: loan, interestRatePct: rate, loanYears: years, overrides: overrides)) else { return }
        result = try? JSONDecoder().decode(Resp.self, from: data).result
    }
}

/// 万円単位で編集する金額フィールド
struct ManField: View {
    let label: String
    @Binding var yen: Double
    var body: some View {
        HStack {
            Text(label)
            Spacer()
            TextField("0", value: Binding(get: { (yen / 10_000).rounded() }, set: { yen = $0 * 10_000 }), format: .number)
                .keyboardType(.numberPad).multilineTextAlignment(.trailing).frame(width: 110)
            Text("万円").foregroundStyle(.secondary)
        }
    }
}
