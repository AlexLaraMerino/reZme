// Render the empty initial interface for documentation; no user data or network.
import AppKit
import SwiftUI

@main
struct Preview {
    @MainActor static func main() throws {
        _ = NSApplication.shared
        let view = NSHostingView(rootView: ContentView().environment(\.colorScheme, .light))
        view.frame = NSRect(x: 0, y: 0, width: 1050, height: 740)
        view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { fatalError("No bitmap") }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
    }
}
