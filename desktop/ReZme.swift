import SwiftUI
import AppKit
import Security

let accent = Color(red: 0.23, green: 0.39, blue: 0.30)
/// Fondo suave para tarjetas y barra lateral (visible en claro y en oscuro).
let panel = Color.primary.opacity(0.045)

/// La clave de la API se guarda en el Llavero de macOS, nunca en disco en claro.
enum KeyStore {
    private static let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "com.almatechnologies.rezme",
        kSecAttrAccount as String: "meta-api-key",
    ]
    static func load() -> String {
        var search = query
        search[kSecReturnData as String] = true
        search[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(search as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    @discardableResult static func save(_ key: String) -> Bool {
        let data = Data(key.utf8)
        let status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item[kSecValueData as String] = data
            return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
        }
        return status == errSecSuccess
    }
    static func delete() { SecItemDelete(query as CFDictionary) }
}

enum Pane: Hashable { case queue, library, report, settings }

struct QueueJob: Identifiable {
    let id: Int
    let title: String
    let state: String
    let detail: String
}

struct LibrarySource: Identifiable {
    let id: Int
    let title: String
    let subtitle: String
    let detail: String
    let verified: Int
    let url: String
}

struct ClaimHit: Identifiable {
    let id: Int
    let statement: String
    let meta: String
}

@MainActor
final class AppModel: ObservableObject {
    private let defaults = UserDefaults.standard
    /// Falso en la vista previa de documentación: no se lanza ningún proceso.
    var live = true

    @Published var pane: Pane = .queue
    @Published var status = ""
    @Published var error = false
    @Published var busy: Pane? = nil

    // Cola
    @Published var queueText = ""
    @Published var jobs: [QueueJob] = []
    @Published var counts: [String: Int] = [:]

    // Base de conocimiento
    @Published var sources: [LibrarySource] = []
    @Published var stats: [String: Int] = [:]
    @Published var query = ""
    @Published var hits: [ClaimHit]? = nil

    // Informe rápido
    @Published var url = ""
    @Published var mode = "prompt"
    @Published var transcript = ""
    @Published var output = ""
    @Published var title = ""

    // Ajustes
    @Published var key = ""
    @Published var model: String { didSet { defaults.set(model, forKey: "model") } }
    @Published var browser: String { didSet { defaults.set(browser, forKey: "browser") } }
    @Published var whisper: Bool { didSet { defaults.set(whisper, forKey: "whisper") } }

    private var process: Process?
    private var generation = UUID()
    private var savedKey = ""
    private var keyLoaded = false

    init() {
        model = defaults.string(forKey: "model") ?? "muse-spark-1.3"
        browser = defaults.string(forKey: "browser") ?? ""
        whisper = defaults.bool(forKey: "whisper")
    }

    var root: URL { Bundle.main.bundleURL.resolvingSymlinksInPath().deletingLastPathComponent() }
    var database: URL { root.appendingPathComponent("rezme_data/rezme.db") }
    var waiting: Int { (counts["pending"] ?? 0) + (counts["running"] ?? 0) }

    private func fail(_ message: String) { error = true; status = message }

    // MARK: Proceso de trabajo

    /// Lanza worker.py. Con `owner`, la tarea ocupa la app y se puede cancelar; sin él, es una consulta breve.
    @discardableResult
    private func launch(_ payload: [String: String], owner: Pane?,
                        onEvent: @escaping @MainActor ([String: Any]) -> Void,
                        onExit: @escaping @MainActor (Bool) -> Void = { _ in }) -> Bool {
        guard live else { return false }
        let runtime = root.appendingPathComponent(".venv/bin/python")
        guard let resources = Bundle.main.resourceURL, FileManager.default.isExecutableFile(atPath: runtime.path) else {
            if owner != nil { fail("Falta el entorno de Python. Ejecuta Instalar.command desde la carpeta del proyecto.") }
            return false
        }
        let task = Process()
        task.executableURL = runtime
        task.arguments = [resources.appendingPathComponent("worker.py").path]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        task.environment = env
        let input = Pipe(), result = Pipe()
        task.standardInput = input; task.standardOutput = result
        task.standardError = FileHandle.nullDevice
        let id = UUID()
        let tracked = owner != nil
        do {
            try task.run()
            try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: payload))
            try input.fileHandleForWriting.close()
        } catch {
            task.terminate()
            if tracked { fail("No se pudo iniciar la tarea: \(error.localizedDescription)") }
            return false
        }
        if tracked { generation = id; process = task; busy = owner }
        DispatchQueue.global(qos: .userInitiated).async {
            var pending = Data()
            while true {
                let data = result.fileHandleForReading.availableData
                if data.isEmpty { break }
                pending.append(data)
                while let end = pending.firstIndex(of: 10) {
                    let line = Data(pending[..<end]); pending.removeSubrange(...end)
                    guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
                    DispatchQueue.main.async {
                        if tracked && self.generation != id { return }
                        onEvent(event)
                    }
                }
            }
            task.waitUntilExit()
            let ok = task.terminationStatus == 0
            DispatchQueue.main.async {
                if tracked {
                    guard self.generation == id else { return }
                    self.busy = nil; self.process = nil
                }
                onExit(ok)
            }
        }
        return true
    }

    func cancel() {
        let owner = busy
        generation = UUID(); process?.terminate(); process = nil; busy = nil; error = false
        if owner == .queue {
            status = "Cola en pausa. Lo ya guardado se conserva."
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.queue("status"); self.loadLibrary() }
        } else {
            status = "Análisis cancelado."
        }
    }

    // MARK: Cola

    /// `run` añade las URLs y procesa la cola; `status`, `retry` y `clear` la gestionan.
    func queue(_ action: String) {
        let tracked = action == "run"
        if tracked {
            guard busy == nil else { return }
            error = false; status = "Preparando la cola…"
        }
        launch(["mode": "queue", "action": action, "urls": queueText, "browser": browser,
                "whisper": whisper ? "1" : "", "db": database.path],
               owner: tracked ? .queue : nil,
               onEvent: { event in
                   switch event["type"] as? String {
                   case "progress", "done": self.status = event["message"] as? String ?? ""
                   case "queue":
                       self.counts = event["counts"] as? [String: Int] ?? [:]
                       self.jobs = (event["jobs"] as? [[String: Any]] ?? []).compactMap { job in
                           guard let id = job["id"] as? Int else { return nil }
                           return QueueJob(id: id, title: job["title"] as? String ?? "",
                                           state: job["state"] as? String ?? "", detail: job["detail"] as? String ?? "")
                       }
                   case "error": self.fail(event["message"] as? String ?? "No se pudo procesar la cola.")
                   default: break
                   }
               },
               onExit: { ok in
                   guard tracked else { return }
                   if ok && !self.error { self.queueText = "" }
                   else if !ok && !self.error { self.fail("El proceso se ha interrumpido. Vuelve a lanzarlo: continuará donde lo dejó.") }
                   self.loadLibrary()
               })
    }

    // MARK: Base de conocimiento

    func loadLibrary(search: Bool = false) {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { hits = nil }
        launch(["mode": "library", "db": database.path, "query": search ? text : ""], owner: nil, onEvent: { event in
            switch event["type"] as? String {
            case "library":
                self.stats = event["stats"] as? [String: Int] ?? [:]
                self.sources = (event["sources"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int else { return nil }
                    let parts = [item["channel"] as? String ?? "", item["date"] as? String ?? ""].filter { !$0.isEmpty }
                    return LibrarySource(id: id, title: item["title"] as? String ?? "", subtitle: parts.joined(separator: " · "),
                                         detail: item["detail"] as? String ?? "", verified: item["verified"] as? Int ?? 0,
                                         url: item["url"] as? String ?? "")
                }
            case "hits":
                self.hits = (event["items"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int else { return nil }
                    return ClaimHit(id: id, statement: item["statement"] as? String ?? "", meta: item["meta"] as? String ?? "")
                }
            default: break
            }
        })
    }

    func revealDatabase() {
        if FileManager.default.fileExists(atPath: database.path) {
            NSWorkspace.shared.activateFileViewerSelecting([database])
        } else {
            status = "La base se creará al guardar el primer vídeo."; error = false
        }
    }

    // MARK: Informe rápido

    func start() {
        guard busy == nil else { status = "Hay una tarea en curso. Espera a que termine o páusala."; error = true; return }
        error = false
        guard let address = URL(string: url.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"].contains(address.host ?? ""),
              ["http", "https"].contains(address.scheme ?? "") else {
            fail("Introduce una URL válida de YouTube."); return
        }
        if mode == "api" {
            loadKey()
            let current = key.trimmingCharacters(in: .whitespacesAndNewlines)
            if current.isEmpty { pane = .settings; fail("Introduce tu clave de Meta en los ajustes."); return }
            saveKey()
        }
        let chosen = mode
        let started = launch(["url": url, "mode": mode, "key": key.trimmingCharacters(in: .whitespacesAndNewlines),
                              "model": model, "browser": browser, "transcript": transcript],
                             owner: .report,
                             onEvent: { event in
                                 switch event["type"] as? String {
                                 case "progress": self.status = event["message"] as? String ?? ""
                                 case "result":
                                     self.output = event["text"] as? String ?? ""
                                     self.title = event["title"] as? String ?? "Resultado"
                                     self.status = chosen == "prompt" ? "Prompt listo. Cópialo y pégalo en tu IA." : "Informe listo."
                                 case "error": self.fail(event["message"] as? String ?? "No se pudo completar el análisis.")
                                 default: break
                                 }
                             },
                             onExit: { ok in
                                 if !ok && !self.error { self.fail("El proceso se ha interrumpido. Puedes volver a intentarlo.") }
                             })
        if started { status = "Preparando el vídeo…" }
    }
    func copy() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(output, forType: .string)
        status = "Copiado al portapapeles."; error = false
    }
    func save() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "reZme-\(mode == "prompt" ? "prompt" : "informe").md"
        panel.canCreateDirectories = true
        if panel.runModal() == .OK, let target = panel.url {
            do { try output.write(to: target, atomically: true, encoding: .utf8); status = "Archivo guardado."; error = false }
            catch { fail("No se pudo guardar el archivo.") }
        }
    }

    // MARK: Clave

    /// Lee la clave guardada la primera vez que hace falta (no al abrir la app).
    func loadKey() {
        guard live, !keyLoaded else { return }
        keyLoaded = true
        savedKey = KeyStore.load()
        if key.isEmpty { key = savedKey }
    }
    func saveKey() {
        let current = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !current.isEmpty, current != savedKey else { return }
        if KeyStore.save(current) { savedKey = current; status = "Clave guardada en el Llavero."; error = false }
        else { fail("No se pudo guardar la clave en el Llavero; se usará solo en esta sesión.") }
    }
    func forgetKey() {
        KeyStore.delete()
        key = ""; savedKey = ""; keyLoaded = true
        status = "Clave eliminada del Llavero."; error = false
    }
}

// MARK: - Interfaz

struct ContentView: View {
    @StateObject private var app: AppModel
    init(model: AppModel? = nil) { _app = StateObject(wrappedValue: model ?? AppModel()) }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                Group {
                    switch app.pane {
                    case .queue: queuePane
                    case .library: libraryPane
                    case .report: reportPane
                    case .settings: settingsPane
                    }
                }
                .padding(.horizontal, 34).padding(.top, 30).padding(.bottom, 18)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                if !app.status.isEmpty { statusBar }
            }.background(Color(nsColor: .textBackgroundColor))
        }
        .frame(minWidth: 980, minHeight: 700)
        .onAppear { app.queue("status"); app.loadLibrary() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in app.cancel() }
    }

    // MARK: Barra lateral

    var sidebar: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 9) {
                Image(systemName: "play.rectangle.fill").font(.system(size: 24)).foregroundStyle(accent)
                Text("reZme").font(.system(size: 25, weight: .bold, design: .rounded))
            }.padding(.bottom, 2)
            Text("Vídeos convertidos en\nconocimiento consultable.").font(.caption).foregroundStyle(.secondary)
                .padding(.bottom, 18)
            navItem(.queue, "Cola", "tray.and.arrow.down", badge: app.waiting)
            navItem(.library, "Base de conocimiento", "cylinder.split.1x2", badge: 0)
            navItem(.report, "Informe rápido", "doc.text", badge: 0)
            Spacer()
            navItem(.settings, "Ajustes", "gearshape", badge: 0)
            Text("Todo se guarda en tu Mac.").font(.caption2).foregroundStyle(.secondary).padding(.top, 8).padding(.leading, 4)
        }
        .padding(20).frame(width: 258, alignment: .leading)
        .frame(maxHeight: .infinity).background(panel)
    }

    func navItem(_ pane: Pane, _ title: String, _ icon: String, badge: Int) -> some View {
        let selected = app.pane == pane
        return Button {
            app.pane = pane
            if pane == .library { app.loadLibrary(search: !app.query.isEmpty) }
            if pane == .queue && app.busy != .queue { app.queue("status") }
            if pane == .settings { app.loadKey() }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: icon).frame(width: 20)
                Text(title).font(.system(size: 13, weight: selected ? .semibold : .regular)).lineLimit(1)
                Spacer()
                if app.busy == pane { ProgressView().controlSize(.small).scaleEffect(0.7) }
                else if badge > 0 {
                    Text("\(badge)").font(.caption2.weight(.semibold)).padding(.horizontal, 7).padding(.vertical, 2)
                        .background(accent.opacity(0.15), in: Capsule())
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? accent.opacity(0.12) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
            .contentShape(Rectangle())
        }.buttonStyle(.plain).foregroundStyle(selected ? accent : Color.primary)
    }

    // MARK: Piezas comunes

    func header(_ title: String, _ subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.system(size: 27, weight: .semibold, design: .serif))
            Text(subtitle).font(.callout).foregroundStyle(.secondary)
        }.padding(.bottom, 18)
    }

    func tile(_ value: Int, _ label: String, _ color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value.formatted(.number.locale(Locale(identifier: "es_ES")))).font(.system(size: 24, weight: .semibold, design: .rounded))
                .foregroundStyle(value == 0 ? Color.secondary : color)
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14).padding(.vertical, 11).frame(maxWidth: .infinity, alignment: .leading)
        .background(panel, in: RoundedRectangle(cornerRadius: 10))
    }

    /// Lista con separadores finos; sustituye a List para controlar el aspecto.
    func rows<Item: Identifiable, Row: View>(_ items: [Item], @ViewBuilder row: @escaping (Item) -> Row) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(items) { item in
                    row(item).padding(.vertical, 9).frame(maxWidth: .infinity, alignment: .leading)
                    Divider().opacity(0.6)
                }
            }
        }
    }

    func emptyState(_ icon: String, _ title: String, _ text: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 34, weight: .light)).foregroundStyle(accent)
            Text(title).font(.system(size: 19, weight: .semibold, design: .serif))
            Text(text).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center).lineSpacing(3)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    var statusBar: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: app.error ? "exclamationmark.circle" : "info.circle")
            Text(app.status).textSelection(.enabled).lineLimit(3)
            Spacer()
        }
        .font(.callout).foregroundStyle(app.error ? Color.red : Color.secondary)
        .padding(.horizontal, 34).padding(.vertical, 11)
        .background((app.error ? Color.red : accent).opacity(0.07))
    }

    // MARK: Cola

    var queuePane: some View {
        VStack(alignment: .leading, spacing: 0) {
            header("Cola de vídeos", "Pega una lista de reproducción o varias URLs. reZme guarda la transcripción de cada vídeo en tu base.")
            VStack(alignment: .leading, spacing: 10) {
                ZStack(alignment: .topLeading) {
                    TextEditor(text: $app.queueText).font(.system(size: 12)).scrollContentBackground(.hidden).frame(height: 58)
                    if app.queueText.isEmpty {
                        Text(verbatim: "https://www.youtube.com/playlist?list=…  ·  una URL por línea")
                            .font(.system(size: 12)).foregroundStyle(.tertiary).padding(.leading, 5).allowsHitTesting(false)
                    }
                }
                Divider()
                HStack {
                    Toggle("Transcribir con Whisper los vídeos sin subtítulos", isOn: $app.whisper)
                        .toggleStyle(.checkbox).font(.caption).disabled(app.busy == .queue)
                    Spacer()
                    if app.busy == .queue {
                        Button("Pausar", action: app.cancel)
                    } else {
                        Button { app.queue("run") } label: {
                            Label(app.queueText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && app.waiting > 0 ? "Procesar la cola" : "Añadir y procesar", systemImage: "arrow.right")
                                .padding(.horizontal, 4)
                        }
                        .buttonStyle(.borderedProminent).tint(accent).keyboardShortcut(.return, modifiers: .command)
                        .disabled(app.busy != nil || (app.queueText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && app.waiting == 0))
                    }
                }
            }
            .padding(13).background(panel, in: RoundedRectangle(cornerRadius: 12))
            .padding(.bottom, 18)

            if app.jobs.isEmpty {
                emptyState("tray", "La cola está vacía", "Añade una lista y reZme irá guardando sus vídeos uno a uno.\nPuedes pausar y continuar cuando quieras.")
            } else {
                HStack(spacing: 10) {
                    tile(app.counts["saved"] ?? 0, "Guardados", accent)
                    tile(app.waiting, "En cola")
                    tile(app.counts["failed"] ?? 0, "Fallidos", .red)
                    tile(app.counts["skipped"] ?? 0, "Sin subtítulos", .orange)
                }.padding(.bottom, 14)
                HStack {
                    Text("VÍDEOS").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Spacer()
                    Button { app.queue("retry") } label: { Label("Reintentar", systemImage: "arrow.clockwise") }
                        .disabled(app.busy == .queue || (app.counts["failed"] ?? 0) + (app.counts["skipped"] ?? 0) == 0)
                    Button { app.queue("clear") } label: { Label("Quitar guardados", systemImage: "checkmark.circle") }
                        .disabled(app.busy == .queue || (app.counts["saved"] ?? 0) == 0)
                }.controlSize(.small).padding(.bottom, 6)
                rows(app.jobs) { job in
                    HStack(alignment: .top, spacing: 10) {
                        Group {
                            switch job.state {
                            case "saved": Image(systemName: "checkmark.circle.fill").foregroundStyle(accent)
                            case "running": ProgressView().controlSize(.small)
                            case "failed": Image(systemName: "xmark.circle.fill").foregroundStyle(Color.red)
                            case "skipped": Image(systemName: "forward.circle").foregroundStyle(Color.orange)
                            default: Image(systemName: "circle").foregroundStyle(Color.secondary)
                            }
                        }.frame(width: 18)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(job.title).lineLimit(1)
                            Text(job.detail).font(.caption).foregroundStyle(job.state == "failed" ? Color.red : Color.secondary).lineLimit(2)
                        }
                    }
                }
            }
        }
    }

    // MARK: Base de conocimiento

    var libraryPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            header("Base de conocimiento", "Lo que reZme ha guardado y verificado, listo para consultar.")
            HStack(spacing: 10) {
                tile(app.stats["videos"] ?? 0, "Vídeos con transcripción", accent)
                tile(app.stats["verified"] ?? 0, "Afirmaciones verificadas", accent)
                tile(app.stats["ungrounded"] ?? 0, "Sin verificar", .orange)
                tile(app.stats["entities"] ?? 0, "Entidades")
            }.padding(.bottom, 14)
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Buscar en las afirmaciones verificadas", text: $app.query).textFieldStyle(.plain)
                    .onSubmit { app.loadLibrary(search: true) }
                if !app.query.isEmpty {
                    Button { app.query = ""; app.hits = nil } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 11).padding(.vertical, 8)
            .background(panel, in: RoundedRectangle(cornerRadius: 9))
            .padding(.bottom, 14)

            if let hits = app.hits {
                Text("\(hits.count) RESULTADOS").font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.bottom, 6)
                if hits.isEmpty {
                    emptyState("magnifyingglass", "Sin resultados", "Solo se busca en afirmaciones verificadas y vigentes.")
                } else {
                    rows(hits) { hit in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(hit.statement).textSelection(.enabled)
                            Text(hit.meta).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            } else if app.sources.isEmpty {
                emptyState("cylinder.split.1x2", "Aún no hay nada guardado", "Añade vídeos desde la cola y aparecerán aquí\ncon su transcripción.")
            } else {
                Text("VÍDEOS GUARDADOS").font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.bottom, 6)
                rows(app.sources) { source in
                    HStack(alignment: .top, spacing: 10) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(source.title).lineLimit(1)
                            Text([source.subtitle, source.detail].filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer()
                        Text(source.verified > 0 ? "\(source.verified) afirmaciones" : "Solo transcripción")
                            .font(.caption).foregroundStyle(source.verified > 0 ? accent : Color.secondary)
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background((source.verified > 0 ? accent : Color.gray).opacity(0.12), in: Capsule())
                    }
                }
                if (app.stats["verified"] ?? 0) == 0 {
                    Text("Las afirmaciones se extraen por ahora desde la terminal: python -m rezme queue run --stage all")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled).padding(.top, 8)
                }
            }
            HStack(spacing: 8) {
                Image(systemName: "internaldrive").foregroundStyle(.secondary)
                Text(app.database.path).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("Mostrar en Finder", action: app.revealDatabase).controlSize(.small)
            }.padding(.top, 10)
        }
    }

    // MARK: Informe rápido

    var reportPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            header("Informe rápido", "Esquema, tesis y highlights de un solo vídeo. No se guarda en la base.")
            VStack(alignment: .leading, spacing: 10) {
                TextField("Pega una URL de YouTube", text: $app.url).textFieldStyle(.roundedBorder)
                HStack {
                    Picker("", selection: $app.mode) {
                        Text("Preparar prompt").tag("prompt")
                        Text("Informe con Muse Spark").tag("api")
                    }.pickerStyle(.segmented).labelsHidden().frame(width: 330).disabled(app.busy == .report)
                    Spacer()
                    if app.busy == .report {
                        ProgressView().controlSize(.small)
                        Button("Cancelar", action: app.cancel)
                    } else {
                        Button(action: app.start) {
                            Label(app.mode == "prompt" ? "Preparar prompt" : "Generar informe", systemImage: "arrow.right").padding(.horizontal, 4)
                        }.buttonStyle(.borderedProminent).tint(accent).keyboardShortcut(.return, modifiers: .command).disabled(app.busy != nil)
                    }
                }
                DisclosureGroup("O pega una transcripción") {
                    TextEditor(text: $app.transcript).font(.system(size: 11)).frame(height: 70)
                        .overlay(RoundedRectangle(cornerRadius: 5).stroke(.quaternary)).padding(.top, 6)
                }.font(.caption)
            }
            .padding(13).background(panel, in: RoundedRectangle(cornerRadius: 12))
            .padding(.bottom, 16)

            if app.output.isEmpty {
                emptyState("text.alignleft", "Tu espacio de lectura",
                           app.mode == "prompt" ? "El prompt no necesita clave: cópialo a la IA que prefieras."
                                                : "El informe usa tu clave de Meta, que se guarda en Ajustes.")
            } else {
                HStack {
                    Text(app.title).font(.title3.weight(.semibold)).lineLimit(1)
                    Spacer()
                    Button(action: app.copy) { Label("Copiar", systemImage: "doc.on.doc") }
                    Button(action: app.save) { Label("Guardar", systemImage: "square.and.arrow.down") }
                }.padding(.bottom, 8)
                TextEditor(text: $app.output).font(.system(size: 13, design: .monospaced)).lineSpacing(4)
                    .scrollContentBackground(.hidden)
            }
        }
    }

    // MARK: Ajustes

    func setting<Content: View>(_ title: String, _ note: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 13, weight: .semibold))
            content()
            Text(note).font(.caption).foregroundStyle(.secondary)
        }
        .padding(14).frame(maxWidth: 560, alignment: .leading)
        .background(panel, in: RoundedRectangle(cornerRadius: 12))
    }

    var settingsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header("Ajustes", "Se aplican a la cola y al informe rápido.").padding(.bottom, -6)
                setting("Clave API de Meta", "Solo para «Informe con Muse Spark». Se guarda en el Llavero de macOS, nunca en un fichero.") {
                    HStack {
                        SecureField("Pega tu clave", text: $app.key).textFieldStyle(.roundedBorder).onSubmit(app.saveKey)
                        Button("Guardar", action: app.saveKey).disabled(app.key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        Button("Olvidar", action: app.forgetKey).disabled(app.key.isEmpty)
                    }
                }
                setting("Modelo", "Identificador del modelo de Meta para el informe.") {
                    TextField("muse-spark-1.3", text: $app.model).textFieldStyle(.roundedBorder).frame(width: 260)
                }
                setting("Sesión de YouTube", "Opcional: usa la sesión de tu navegador si YouTube pide iniciar sesión o limita las descargas.") {
                    Picker("", selection: $app.browser) {
                        Text("Sin navegador").tag("")
                        Text("Chrome").tag("chrome")
                        Text("Firefox").tag("firefox")
                        Text("Safari").tag("safari")
                    }.labelsHidden().fixedSize()
                }
                setting("Vídeos sin subtítulos", "Whisper transcribe el audio en tu Mac. Con vídeos largos puede tardar horas; si lo desactivas, quedan marcados para después.") {
                    Toggle("Transcribir el audio con Whisper", isOn: $app.whisper).toggleStyle(.checkbox)
                }
                setting("Base de datos", "Un único fichero SQLite con transcripciones, afirmaciones y la cola.") {
                    HStack {
                        Text(app.database.path).font(.caption).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("Mostrar en Finder", action: app.revealDatabase).controlSize(.small)
                    }
                }
            }
        }
    }
}

@main
struct ReZmeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        Window("reZme", id: "main") { ContentView() }
            .defaultSize(width: 1080, height: 740)
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
