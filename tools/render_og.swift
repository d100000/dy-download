#!/usr/bin/env swift

import AppKit
import Foundation

guard CommandLine.arguments.count == 3 else {
    fputs("usage: render_og.swift INPUT.svg OUTPUT.png\n", stderr)
    exit(2)
}

let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2])
guard let source = NSImage(contentsOf: input) else {
    fputs("cannot load SVG\n", stderr)
    exit(1)
}

let width = 1200
let height = 630
guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: width,
    pixelsHigh: height,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
), let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
    fputs("cannot create bitmap context\n", stderr)
    exit(1)
}

bitmap.size = NSSize(width: width, height: height)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
NSColor.black.setFill()
NSRect(x: 0, y: 0, width: width, height: height).fill()
source.draw(
    in: NSRect(x: 0, y: 0, width: width, height: height),
    from: NSRect(origin: .zero, size: source.size),
    operation: .copy,
    fraction: 1
)
context.flushGraphics()
NSGraphicsContext.restoreGraphicsState()

guard let png = bitmap.representation(using: .png, properties: [:]) else {
    fputs("cannot encode PNG\n", stderr)
    exit(1)
}
try png.write(to: output, options: .atomic)
