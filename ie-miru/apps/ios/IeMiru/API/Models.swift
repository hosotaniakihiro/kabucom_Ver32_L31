import Foundation

/// API レスポンスのうち iOS で使う部分（apps/api の JSON と同じキー）。
struct LatLngDTO: Codable, Hashable { var lat: Double; var lng: Double }

struct SourceRefDTO: Codable, Hashable {
    var id: String
    var name: String
    var url: String?
    var license: String?
    var mode: String   // live | mock | demo | cache
}

struct BuildingDTO: Codable, Identifiable, Hashable {
    var id: String
    var source: SourceRefDTO
    var footprint: [LatLngDTO]
    var centroid: LatLngDTO
    var footprintAreaM2: Double
    var heightM: Double?
    var floorsAbove: Int?
    var usage: String?
    var structure: String?
    var builtYear: Int?
}

struct CandidateDTO: Codable, Hashable {
    var building: BuildingDTO
    var distanceM: Double
    var bearingDeg: Double
    var angleOffsetDeg: Double?
    var inView: Bool
    var hitOrder: Int?
    var score: Double
    var reasons: [String]
}

struct SelectionDTO: Codable {
    var mode: String       // suggest | choose | manual
    var message: String
    var primaryId: String?
    var candidates: [CandidateDTO]
    var usedHeading: Bool
}

struct AssessmentDTO: Codable {
    var usable: Bool
    var message: String?
    var reason: String?
}

struct CandidatesResponse: Codable {
    var assessment: AssessmentDTO
    var selection: SelectionDTO?
    var sources: [SourceRefDTO]
}

/// 値 + 出自。status が available 以外のとき value は nil（0 ではない）
struct Sourced<T: Codable>: Codable {
    var status: String     // available | no_data | not_applicable | unavailable
    var value: T?
    var kind: String       // public | ai_estimate | user | reference
    var sources: [SourceRefDTO]
    var reason: String?
    var note: String?

    var statusLabel: String {
        switch status {
        case "no_data": return "データなし"
        case "not_applicable": return "対象外"
        case "unavailable": return "確認できず"
        default: return ""
        }
    }
    var kindLabel: String {
        ["public": "公的データ", "ai_estimate": "AI推定", "user": "ユーザー登録", "reference": "参考情報"][kind] ?? kind
    }
    var isMock: Bool { sources.contains { $0.mode == "mock" || $0.mode == "demo" } }
}

struct ZoningDTO: Codable { var useDistrict: String; var coverageRatioPct: Double?; var floorAreaRatioPct: Double?; var firePrevention: String? }
struct LandPriceDTO: Codable, Hashable { var id: String; var kind: String; var year: Int; var pricePerM2: Double; var yoyChangePct: Double?; var distanceM: Double? }
struct HazardDTO: Codable, Hashable {
    var type: String; var status: String; var level: String?; var severity: Int?; var detail: String?; var reason: String?
    var label: String { ["flood": "洪水", "inland_flood": "内水", "tsunami": "津波", "storm_surge": "高潮", "landslide": "土砂災害", "liquefaction": "液状化"][type] ?? type }
    /// domain/hazard.ts hazardStatusLabel と同じ。「安全」は生成しない
    var statusLabel: String {
        switch status {
        case "in_zone": return level.map { "想定区域内（\($0)）" } ?? "想定区域内"
        case "graded": return level ?? "評価あり"
        case "no_data": return "データなし"
        case "not_applicable": return "対象外"
        default: return "確認できず"
        }
    }
}
struct HazardSummaryDTO: Codable { var headline: String }
struct FactsDTO: Codable {
    var heightM: Sourced<Double>; var floorsAbove: Sourced<Int>; var builtYear: Sourced<Int>
    var usage: Sourced<String>; var structure: Sourced<String>; var footprintAreaM2: Sourced<Double>
}
struct ValuationDTO: Codable { var estimatedLow: Double; var estimatedMid: Double; var estimatedHigh: Double; var confidence: Double; var reasons: [String] }
struct ComparablesDTO: Codable { var count: Int; var medianPriceYen: Double?; var medianUnitPriceYenPerM2: Double? }

struct BuildingReportDTO: Codable {
    var building: BuildingDTO
    var facts: FactsDTO
    var zoning: Sourced<ZoningDTO>
    var landPrices: Sourced<[LandPriceDTO]>
    var hazards: [HazardDTO]
    var hazardSummary: HazardSummaryDTO
    var valuation: Sourced<ValuationDTO>?
    var comparables: Sourced<ComparablesDTO>?
    var dataMode: String
    var disclaimers: [String]
}
