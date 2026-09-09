import AppKit
let image = NSImage(size: NSSize(width: 1024, height: 1024))
image.lockFocus()
NSColor(calibratedRed: 0.18, green: 0.32, blue: 0.24, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 50, y: 50, width: 924, height: 924), xRadius: 210, yRadius: 210).fill()
NSColor(calibratedRed: 0.93, green: 0.94, blue: 0.84, alpha: 1).setFill()
let triangle = NSBezierPath(); triangle.move(to: NSPoint(x: 310, y: 385)); triangle.line(to: NSPoint(x: 310, y: 745)); triangle.line(to: NSPoint(x: 660, y: 565)); triangle.close(); triangle.fill()
for (y, width) in [(300, 400), (210, 280)] { NSBezierPath(roundedRect: NSRect(x: 310, y: y, width: width, height: 38), xRadius: 19, yRadius: 19).fill() }
image.unlockFocus()
let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
