// kadak-vision: reads every .jpg in a folder (name order) and prints one JSON line per frame:
// {"i": n, "text": ["WASTED", ...], "faces": [[x, y, w, h], ...], "thumb": [...]}
// faces are 0..1 with origin top-left; thumb is the frame shrunk to 32x18 grey pixels (0..255), top row first.
import Foundation
import ImageIO
import CoreGraphics
import Vision

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: kadak-vision FRAMES_DIR\n".data(using: .utf8)!)
    exit(2)
}
let dir = URL(fileURLWithPath: args[1])
let names = ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [])
    .filter { $0.lowercased().hasSuffix(".jpg") }.sorted()

for (i, name) in names.enumerated() {
    var texts: [String] = []
    var faces: [[Double]] = []
    var thumb: [Int] = []
    let url = dir.appendingPathComponent(name)
    if let src = CGImageSourceCreateWithURL(url as CFURL, nil),
       let img = CGImageSourceCreateImageAtIndex(src, 0, nil) {
        let textReq = VNRecognizeTextRequest()
        textReq.recognitionLevel = .fast
        textReq.usesLanguageCorrection = false
        let faceReq = VNDetectFaceRectanglesRequest()
        try? VNImageRequestHandler(cgImage: img, options: [:]).perform([textReq, faceReq])
        for obs in textReq.results ?? [] {
            if let s = obs.topCandidates(1).first?.string { texts.append(s) }
        }
        for f in faceReq.results ?? [] {
            let b = f.boundingBox  // Vision: origin bottom-left
            let box: [Double] = [Double(b.minX), Double(1 - b.maxY), Double(b.width), Double(b.height)]
            faces.append(box.map { ($0 * 10000).rounded() / 10000 })
        }
        // A tiny grey copy lets Kadak spot black, frozen and letterboxed (cutscene) screens.
        if let ctx = CGContext(data: nil, width: 32, height: 18, bitsPerComponent: 8, bytesPerRow: 32,
                               space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue) {
            ctx.interpolationQuality = .medium
            ctx.draw(img, in: CGRect(x: 0, y: 0, width: 32, height: 18))
            if let p = ctx.data?.bindMemory(to: UInt8.self, capacity: 32 * 18) {
                thumb = (0..<(32 * 18)).map { Int(p[$0]) }
            }
        }
    }
    let line: [String: Any] = ["i": i, "text": texts, "faces": faces, "thumb": thumb]
    if let data = try? JSONSerialization.data(withJSONObject: line),
       let s = String(data: data, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}
