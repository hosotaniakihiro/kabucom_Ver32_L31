import SwiftUI
import UIKit

/// 「直す」: 写真撮影 → タップでマーキング → カテゴリ・メモ → API（D1 + R2）へ保存
struct FixView: View {
    let building: BuildingDTO
    static let categories: [(String, String)] = [("exterior_wall", "外壁"), ("roof", "屋根"), ("gutter", "雨樋"), ("window", "窓"), ("entrance", "玄関"), ("fence", "塀"), ("other", "その他")]

    @State private var category = "exterior_wall"
    @State private var memo = ""
    @State private var image: UIImage?
    @State private var marks: [CGPoint] = []   // 0..1 正規化
    @State private var showCamera = false
    @State private var saving = false
    @State private var message: String?
    @State private var measurement: Double?    // LiDAR で測った長さ [m]

    var body: some View {
        Form {
            Picker("箇所", selection: $category) { ForEach(Self.categories, id: \.0) { Text($0.1).tag($0.0) } }
            Section("写真") {
                Button(image == nil ? "写真を撮る" : "撮り直す") { showCamera = true }
                if let image {
                    GeometryReader { geo in
                        let size = fitted(image.size, in: geo.size)
                        ZStack(alignment: .topLeading) {
                            Image(uiImage: image).resizable().frame(width: size.width, height: size.height)
                            ForEach(Array(marks.enumerated()), id: \.offset) { i, p in
                                Circle().stroke(Color.red, lineWidth: 3).frame(width: 36, height: 36)
                                    .overlay(Text("\(i + 1)").font(.caption.bold()).foregroundStyle(.red).offset(x: 24, y: -14))
                                    .position(x: p.x * size.width, y: p.y * size.height)
                            }
                        }
                        .contentShape(Rectangle())
                        .onTapGesture { loc in marks.append(CGPoint(x: loc.x / size.width, y: loc.y / size.height)) }
                    }
                    .frame(height: 300)
                    HStack { Text("マーク \(marks.count)件"); Spacer(); Button("1つ戻す") { _ = marks.popLast() } }
                }
            }
            Section("メモ") { TextField("例: 外壁にひび。幅1mmほど", text: $memo, axis: .vertical) }
            if ARMeasureView.isSupported {
                Section("サイズ（LiDAR）") {
                    NavigationLink(measurement.map { String(format: "測定値 %.2f m", $0) } ?? "LiDARで長さを測る") { ARMeasureView { measurement = $0 } }
                }
            }
            Button(saving ? "保存中…" : "保存する") { Task { await save() } }.disabled(saving)
            if let message { Text(message) }
            Section { NavigationLink("ARメモを置く") { ARNoteView(building: building) } }
        }
        .navigationTitle("直す")
        .sheet(isPresented: $showCamera) { CameraPicker(image: $image).ignoresSafeArea() }
        .onChange(of: image) { _, _ in marks = [] }
    }

    private func fitted(_ s: CGSize, in box: CGSize) -> CGSize {
        let k = min(box.width / s.width, box.height / s.height)
        return CGSize(width: s.width * k, height: s.height * k)
    }

    private func save() async {
        saving = true
        defer { saving = false }
        var mp = Multipart()
        mp.field("buildingId", building.id)
        mp.field("category", category)
        mp.field("memo", memo)
        let marksJSON = marks.map { ["x": $0.x, "y": $0.y, "r": 0.05] }
        mp.field("marks", String(data: (try? JSONSerialization.data(withJSONObject: marksJSON)) ?? Data("[]".utf8), encoding: .utf8)!)
        if let m = measurement {
            mp.field("measurements", "[{\"kind\":\"length\",\"value\":\(m),\"method\":\"lidar\"}]")
        }
        if let jpeg = image?.resizedToMax(1600).jpegData(compressionQuality: 0.85) { mp.file("photo", filename: "photo.jpg", type: "image/jpeg", data: jpeg) }
        do {
            _ = try await APIClient.shared.upload("v1/inspections", multipart: mp.finish(), boundary: mp.boundary)
            message = "保存しました"
            memo = ""
        } catch {
            message = "保存できませんでした（通信エラー）"
        }
    }
}

struct Multipart {
    let boundary = "iemiru-\(UUID().uuidString)"
    private var data = Data()
    mutating func field(_ name: String, _ value: String) {
        data.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
    }
    mutating func file(_ name: String, filename: String, type: String, data d: Data) {
        data.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\nContent-Type: \(type)\r\n\r\n".utf8))
        data.append(d)
        data.append(Data("\r\n".utf8))
    }
    func finish() -> Data { data + Data("--\(boundary)--\r\n".utf8) }
}

struct CameraPicker: UIViewControllerRepresentable {
    @Binding var image: UIImage?
    @Environment(\.dismiss) private var dismiss
    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController()
        p.sourceType = UIImagePickerController.isSourceTypeAvailable(.camera) ? .camera : .photoLibrary
        p.delegate = context.coordinator
        return p
    }
    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraPicker
        init(_ p: CameraPicker) { parent = p }
        func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            parent.image = info[.originalImage] as? UIImage
            parent.dismiss()
        }
        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { parent.dismiss() }
    }
}

extension UIImage {
    func resizedToMax(_ maxSide: CGFloat) -> UIImage {
        let k = min(1, maxSide / max(size.width, size.height))
        guard k < 1 else { return self }
        let s = CGSize(width: size.width * k, height: size.height * k)
        return UIGraphicsImageRenderer(size: s).image { _ in draw(in: CGRect(origin: .zero, size: s)) }
    }
}
