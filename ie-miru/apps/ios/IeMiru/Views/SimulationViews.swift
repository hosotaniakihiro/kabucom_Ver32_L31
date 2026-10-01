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

struct SellSimulationView: View {
    let report: BuildingReportDTO
    @State private var price: Double = 0
    @State private var payoff: Double = 0
    @State private var acquisition: Double = 0
    @State private var holding: Double = 0
    @State private var ownHome = true
    @State private var result: SellResultDTO?
    @State private var appraisalMessage: String?

    struct SellResultDTO: Codable { var costs: [CostLineDTO]; var totalCosts: Double; var capitalGainsTax: Double?; var taxNote: String; var netProceeds: Double }
    struct Resp: Codable { var result: SellResultDTO }

    var body: some View {
        Form {
            Section("参考") {
                if let v = report.valuation?.value { LabeledContent("AI参考査定", value: Yen.range(v.estimatedLow, v.estimatedHigh)) } else { Text("AI参考査定: 算出できません") }
                if let c = report.comparables?.value { LabeledContent("周辺取引事例", value: "\(c.count)件") }
                Text("正式な査定ではありません。").font(.caption2)
            }
            Section("条件") {
                ManField(label: "想定売却価格", yen: $price)
                ManField(label: "ローン残債", yen: $payoff)
                ManField(label: "取得費（任意）", yen: $acquisition)
                Stepper("所有期間 \(Int(holding))年", value: $holding, in: 0...80)
                Toggle("居住用3,000万円特別控除", isOn: $ownHome)
            }
            if let r = result {
                Section("結果") {
                    ForEach(r.costs, id: \.key) { LabeledContent($0.label + ($0.estimated ? "（概算）" : ""), value: Yen.man($0.amount)) }
                    LabeledContent("譲渡所得税（概算）", value: r.capitalGainsTax.map { Yen.man($0) } ?? "未計算")
                    Text(r.taxNote).font(.caption2)
                    LabeledContent("手取り概算", value: Yen.man(r.netProceeds)).bold()
                }
            }
            Section("不動産会社に査定を依頼") {
                Button("査定を依頼する") { Task { await requestAppraisal() } }
                if let m = appraisalMessage { Text(m).font(.caption) }
            }
        }
        .navigationTitle("売る")
        .task { price = report.valuation?.value?.estimatedMid ?? 0; await recalc() }
        .onChange(of: [price, payoff, acquisition, holding]) { _, _ in Task { await recalc() } }
        .onChange(of: ownHome) { _, _ in Task { await recalc() } }
    }

    private func recalc() async {
        struct Body: Encodable { var salePrice: Double; var mortgagePayoff: Double; var acquisitionCost: Double?; var holdingYears: Double?; var ownHomeDeduction: Bool }
        let body = Body(salePrice: price, mortgagePayoff: payoff, acquisitionCost: acquisition > 0 ? acquisition : nil, holdingYears: holding > 0 ? holding : nil, ownHomeDeduction: ownHome)
        guard let data = try? await APIClient.shared.send("v1/simulations/sell", method: "POST", json: body) else { return }
        result = try? JSONDecoder().decode(Resp.self, from: data).result
    }

    private func requestAppraisal() async {
        struct Body: Encodable { var buildingId: String }
        struct R: Decodable { struct X: Decodable { var message: String }; var responses: [X] }
        guard let data = try? await APIClient.shared.send("v1/appraisal-requests", method: "POST", json: Body(buildingId: report.building.id)),
              let r = try? JSONDecoder().decode(R.self, from: data) else { appraisalMessage = "通信できませんでした。"; return }
        appraisalMessage = r.responses.map(\.message).joined(separator: " ")
    }
}

struct RentSimulationView: View {
    let report: BuildingReportDTO
    @State private var rent: Double = 0
    @State private var occupancy: Double = 95
    @State private var result: RentResultDTO?
    struct RentResultDTO: Codable { var annualNetIncome: Double; var grossYieldPct: Double?; var netYieldPct: Double?; var notes: [String] }
    struct Resp: Codable { var input: In; var result: RentResultDTO; struct In: Codable { var monthlyRent: Double } }

    var body: some View {
        Form {
            ManField(label: "想定家賃（月額）", yen: $rent)
            Stepper("入居率 \(Int(occupancy))%", value: $occupancy, in: 0...100, step: 5)
            if let r = result {
                LabeledContent("年間手取り", value: Yen.man(r.annualNetIncome)).bold()
                LabeledContent("表面利回り", value: r.grossYieldPct.map { String(format: "%.1f%%", $0) } ?? "算出不可")
                LabeledContent("実質利回り", value: r.netYieldPct.map { String(format: "%.1f%%", $0) } ?? "算出不可")
                ForEach(r.notes, id: \.self) { Text($0).font(.caption2) }
            }
        }
        .navigationTitle("貸す")
        .task { await recalc(initial: true) }
        .onChange(of: [rent, occupancy]) { _, _ in Task { await recalc(initial: false) } }
    }

    private func recalc(initial: Bool) async {
        struct Body: Encodable { var referencePrice: Double?; var monthlyRent: Double?; var occupancyPct: Double }
        let body = Body(referencePrice: report.valuation?.value?.estimatedMid, monthlyRent: initial ? nil : rent, occupancyPct: occupancy)
        guard let data = try? await APIClient.shared.send("v1/simulations/rent", method: "POST", json: body), let r = try? JSONDecoder().decode(Resp.self, from: data) else { return }
        if initial { rent = r.input.monthlyRent }
        result = r.result
    }
}
