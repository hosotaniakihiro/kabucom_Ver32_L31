import Foundation

enum APIError: Error { case network, http(Int, String), decode }

/// apps/api への薄いクライアント。通信不可時は最後に取得したレポートを返す（オフライン表示）。
actor APIClient {
    static let shared = APIClient()
    private let session: URLSession
    private let base = AppConfig.apiBaseURL
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    init(session: URLSession = .shared) { self.session = session }

    private func request(_ path: String, method: String = "GET", body: Data? = nil, contentType: String = "application/json") async throws -> Data {
        // path はクエリを含み得るため appendingPathComponent ではなく文字列で結合する（? のエスケープ防止）
        let root = base.absoluteString.hasSuffix("/") ? String(base.absoluteString.dropLast()) : base.absoluteString
        guard let url = URL(string: "\(root)/\(path)") else { throw APIError.network }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 20
        req.setValue(AppConfig.deviceId, forHTTPHeaderField: AppConfig.deviceHeader)
        if let body { req.httpBody = body; req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        do {
            let (data, res) = try await session.data(for: req)
            let code = (res as? HTTPURLResponse)?.statusCode ?? 0
            guard (200..<300).contains(code) else { throw APIError.http(code, String(data: data, encoding: .utf8) ?? "") }
            return data
        } catch let e as APIError { throw e } catch { throw APIError.network }
    }

    func candidates(fix: LocationFix?, pose: CameraPose?, permission: LocationPermission, measuredDistanceM: Float?) async throws -> CandidatesResponse {
        struct Body: Encodable { var fix: LocationFix?; var pose: CameraPose?; var permission: String; var measuredDistanceM: Float? }
        let data = try await request("v1/candidates", method: "POST", body: try encoder.encode(Body(fix: fix, pose: pose, permission: permission.rawValue, measuredDistanceM: measuredDistanceM)))
        return try decoder.decode(CandidatesResponse.self, from: data)
    }

    func nearby(lat: Double, lng: Double, radius: Int = 120) async throws -> [BuildingDTO] {
        struct R: Decodable { var buildings: [BuildingDTO] }
        return try decoder.decode(R.self, from: try await request("v1/buildings/nearby?lat=\(lat)&lng=\(lng)&radius=\(radius)")).buildings
    }

    func report(buildingId: String) async throws -> (BuildingReportDTO, offline: Bool) {
        let key = "iemiru.report.\(buildingId)"
        let enc = buildingId.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? buildingId
        do {
            let data = try await request("v1/buildings/\(enc)/report")
            UserDefaults.standard.set(data, forKey: key)
            return (try decoder.decode(BuildingReportDTO.self, from: data), false)
        } catch APIError.network {
            if let cached = UserDefaults.standard.data(forKey: key) { return (try decoder.decode(BuildingReportDTO.self, from: cached), true) }
            throw APIError.network
        }
    }

    func send(_ path: String, method: String, json: Encodable?) async throws -> Data {
        try await request(path, method: method, body: try json.map { try encoder.encode($0) })
    }

    func get(_ path: String) async throws -> Data { try await request(path) }

    func delete(_ path: String) async throws -> Data { try await request(path, method: "DELETE") }

    func put(_ path: String, binary: Data) async throws -> Data {
        try await request(path, method: "PUT", body: binary, contentType: "application/octet-stream")
    }

    func upload(_ path: String, multipart: Data, boundary: String) async throws -> Data {
        try await request(path, method: "POST", body: multipart, contentType: "multipart/form-data; boundary=\(boundary)")
    }
}
