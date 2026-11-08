// Reads the Mac's calendars (iCloud, Google, Exchange/Outlook, shared and
// subscribed) through Apple's EventKit and prints today's and the next days'
// events as JSON. Built automatically by "Start Command Center".
//
//   cal-helper [days]      default 2: today and tomorrow
import CoreGraphics
import EventKit
import Foundation

let store = EKEventStore()

func emit(_ obj: [String: Any]) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: []) {
        FileHandle.standardOutput.write(data)
    }
    exit(0)
}

func hexColor(_ cal: EKCalendar) -> String {
    guard let srgb = CGColorSpace(name: CGColorSpace.sRGB),
          let c = cal.cgColor?.converted(to: srgb, intent: .defaultIntent, options: nil),
          let comps = c.components, comps.count >= 3 else { return "" }
    func byte(_ v: CGFloat) -> Int { return max(0, min(255, Int((v * 255).rounded()))) }
    return String(format: "#%02lx%02lx%02lx", byte(comps[0]), byte(comps[1]), byte(comps[2]))
}

func readEvents() -> Never {
    let days = Int(CommandLine.arguments.dropFirst().first ?? "2") ?? 2
    let calendar = Calendar.current
    let start = calendar.startOfDay(for: Date())
    let end = calendar.date(byAdding: .day, value: max(1, min(days, 14)), to: start) ?? start
    let cals = store.calendars(for: .event)
    let iso = ISO8601DateFormatter()
    var events: [[String: Any]] = []
    if !cals.isEmpty {
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: cals)
        for e in store.events(matching: predicate) {
            if e.status == .canceled { continue }
            events.append([
                "title": e.title ?? "",
                "start": iso.string(from: e.startDate),
                "end": iso.string(from: e.endDate),
                "allDay": e.isAllDay,
                "cal": e.calendar.calendarIdentifier,
                "loc": e.location ?? "",
                "notes": String((e.notes ?? "").prefix(4000)),
                "url": e.url?.absoluteString ?? "",
            ])
        }
    }
    let list: [[String: Any]] = cals.map {
        ["id": $0.calendarIdentifier, "title": $0.title, "color": hexColor($0), "account": $0.source.title]
    }
    emit(["ok": true, "calendars": list, "events": events])
}

func hasAccess() -> Bool {
    let status = EKEventStore.authorizationStatus(for: .event)
    if #available(macOS 14.0, *) {
        return status == .fullAccess
    }
    return status.rawValue == 3   // .authorized on macOS 13 and earlier
}

let status = EKEventStore.authorizationStatus(for: .event)
if hasAccess() {
    readEvents()
} else if status == .notDetermined {
    // First run: macOS shows "would like to access your calendars"
    let done: (Bool, Error?) -> Void = { granted, _ in
        if granted { readEvents() }
        emit(["ok": false, "error": "permission"])
    }
    if #available(macOS 14.0, *) {
        store.requestFullAccessToEvents(completion: done)
    } else {
        store.requestAccess(to: .event, completion: done)
    }
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 120))
    emit(["ok": false, "error": "timeout"])
} else {
    emit(["ok": false, "error": "permission"])
}
