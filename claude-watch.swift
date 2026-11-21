// claude-watch: reports whether the Claude desktop app is busy, by reading its
// buttons through macOS Accessibility (the same permission the Notifications
// panel uses). A visible "Stop" button means Claude is working; an "Allow"
// button means it is waiting on you. Prints one line of JSON and exits.
import Cocoa
import ApplicationServices

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}
func str(_ el: AXUIElement, _ name: String) -> String {
    return (attr(el, name) as? String) ?? ""
}

var out: [String: Any] = ["running": false]
let apps = NSWorkspace.shared.runningApplications.filter {
    $0.bundleIdentifier == "com.anthropic.claudefordesktop" || $0.localizedName == "Claude"
}
if let app = apps.first {
    out["running"] = true
    out["trusted"] = AXIsProcessTrusted()
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(ax, 1.0)
    // Electron apps only build their accessibility tree when asked to
    AXUIElementSetAttributeValue(ax, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    var titles: [String] = []
    var labels: [String] = []
    var nodes = 0
    let deadline = Date().addingTimeInterval(4)
    func walk(_ el: AXUIElement, _ depth: Int) {
        if nodes > 12000 || depth > 80 || Date() > deadline { return }
        nodes += 1
        let role = str(el, "AXRole")
        if role == "AXButton" {
            let l = [str(el, "AXDescription"), str(el, "AXTitle"), str(el, "AXHelp")].filter { !$0.isEmpty }.joined(separator: " | ")
            if !l.isEmpty && !labels.contains(l) { labels.append(l) }
        }
        if let kids = attr(el, "AXChildren") as? [AXUIElement] {
            for k in kids { walk(k, depth + 1) }
        }
    }
    if let wins = attr(ax, "AXWindows") as? [AXUIElement] {
        for w in wins {
            titles.append(str(w, "AXTitle"))
            walk(w, 0)
        }
    }
    let low = labels.map { $0.lowercased() }
    out["working"] = low.contains { $0.hasPrefix("stop") || $0.contains("stop response") || $0.contains("stop generating") || $0.contains("interrupt") }
    out["waiting"] = low.contains { $0 == "allow" || $0.hasPrefix("allow once") || $0.hasPrefix("always allow") || $0.hasPrefix("approve") }
    out["windows"] = titles
    out["buttons"] = Array(labels.prefix(80))
    out["nodes"] = nodes
}
if let data = try? JSONSerialization.data(withJSONObject: out), let s = String(data: data, encoding: .utf8) {
    print(s)
}
