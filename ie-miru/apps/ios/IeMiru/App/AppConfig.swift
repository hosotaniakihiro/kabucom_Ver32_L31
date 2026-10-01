import Foundation

/// 名称・接続先の単一ソース（packages/config/src/brand.ts と同じ値を保つ）。
enum AppConfig {
    static let codeName = "ie-miru"
    static var displayName: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "家を見るAI"
    }
    static var apiBaseURL: URL {
        let raw = Bundle.main.object(forInfoDictionaryKey: "IEMIRU_API_BASE_URL") as? String
        return URL(string: raw ?? "") ?? URL(string: "http://localhost:8787")!
    }
    static let deviceHeader = "X-IeMiru-Device"

    /// 匿名端末ID（個人情報を含まない UUID）
    static var deviceId: String {
        let key = "iemiru.deviceId"
        if let v = UserDefaults.standard.string(forKey: key) { return v }
        let v = UUID().uuidString
        UserDefaults.standard.set(v, forKey: key)
        return v
    }
}
