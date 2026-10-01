import SwiftUI
import AppKit
import Security

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

@MainActor
final class AppModel: ObservableObject {
    @Published var url = ""
    @Published var mode = "prompt"
    @Published var key = ""
    @Published var model = "muse-spark-1.3"
    @Published var browser = ""
    @Published var transcript = ""
    @Published var output = ""
    @Published var title = "Tu próxima idea empieza aquí."
    @Published var status = ""
    @Published var busy = false
    @Published var error = false
    @Published var options = false
    var process: Process?
    var generation = UUID()
    private var savedKey = ""
    private var keyLoaded = false

    /// Lee la clave guardada la primera vez que hace falta (no al abrir la app).
    func loadKey() {
        guard !keyLoaded else { return }
        keyLoaded = true
        savedKey = KeyStore.load()
        if key.isEmpty { key = savedKey }
    }
    func forgetKey() {
        KeyStore.delete()
        key = ""; savedKey = ""; keyLoaded = true
        status = "Clave eliminada del Llavero."; error = false
    }

    func start() {
        guard !busy else { return }
        error = false
        if mode == "api" { loadKey() }
        guard let address = URL(string: url.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"].contains(address.host ?? ""),
              ["http", "https"].contains(address.scheme ?? "") else {
            status = "Introduce una URL válida de YouTube."; error = true; return
        }
        if mode == "api" && key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            options = true; status = "Introduce tu clave de Meta en las opciones."; error = true; return
        }
        if mode == "api" {
            let current = key.trimmingCharacters(in: .whitespacesAndNewlines)
            if current != savedKey {
                if KeyStore.save(current) { savedKey = current }
                else { status = "No se pudo guardar la clave en el Llavero; se usará solo en esta sesión." }
            }
        }
        guard let resources = Bundle.main.resourceURL else {
            status = "No se encuentra el entorno de la aplicación."; error = true; return
        }
        let runtime = Bundle.main.bundleURL.resolvingSymlinksInPath().deletingLastPathComponent().appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: runtime.path) else {
            status = "Falta el entorno de Python. Ejecuta Instalar.command desde la carpeta del proyecto."; error = true; return
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
        let payload: [String: String] = ["url": url, "mode": mode, "key": key.trimmingCharacters(in: .whitespacesAndNewlines), "model": model, "browser": browser, "transcript": transcript]
        let id = UUID(); generation = id
        do {
            try task.run()
            process = task; busy = true; status = "Preparando el vídeo…"
            try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: payload))
            try input.fileHandleForWriting.close()
        } catch {
            task.terminate(); busy = false; self.error = true
            status = "No se pudo iniciar el análisis: \(error.localizedDescription)"; return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            var pending = Data()
            while true {
                let data = result.fileHandleForReading.availableData
                if data.isEmpty { break }
                pending.append(data)
                while let end = pending.firstIndex(of: 10) {
                    let line = Data(pending[..<end]); pending.removeSubrange(...end)
                    guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: String] else { continue }
                    DispatchQueue.main.async {
                        guard self.generation == id else { return }
                        switch event["type"] {
                        case "progress": self.status = event["message"] ?? ""
                        case "result":
                            self.output = event["text"] ?? ""; self.title = event["title"] ?? "Resultado"
                            self.status = payload["mode"] == "prompt" ? "Prompt listo. Cópialo y pégalo en tu IA." : "Informe listo."
                        case "error": self.error = true; self.status = event["message"] ?? "No se pudo completar el análisis."
                        default: break
                        }
                    }
                }
            }
            task.waitUntilExit()
            DispatchQueue.main.async {
                guard self.generation == id else { return }
                self.busy = false; self.process = nil
                if task.terminationStatus != 0 && !self.error {
                    self.error = true; self.status = "El proceso se ha interrumpido. Puedes volver a intentarlo."
                }
            }
        }
    }
    func cancel() {
        generation = UUID(); process?.terminate(); process = nil; busy = false
        status = "Análisis cancelado."; error = false
    }
    func copy() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(output, forType: .string)
        status = "Copiado al portapapeles."
    }
    func save() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "reZme-\(mode == "prompt" ? "prompt" : "informe").md"
        panel.canCreateDirectories = true
        if panel.runModal() == .OK, let target = panel.url {
            do { try output.write(to: target, atomically: true, encoding: .utf8); status = "Archivo guardado." }
            catch { self.error = true; status = "No se pudo guardar el archivo." }
        }
    }
}

struct ContentView: View {
    @StateObject private var app = AppModel()
    let accent = Color(red: 0.23, green: 0.39, blue: 0.30)
    var body: some View {
        HStack(spacing: 0) {
            ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(spacing: 9) {
                    Image(systemName: "play.rectangle.fill").font(.system(size: 28)).foregroundStyle(accent)
                    Text("reZme").font(.system(size: 29, weight: .bold, design: .rounded))
                }
                Text("Menos vídeo. Más ideas.").font(.subheadline).foregroundStyle(.secondary)
                Divider()
                VStack(alignment: .leading, spacing: 9) {
                    Text("01  EL VÍDEO").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    TextField("Pega una URL de YouTube", text: $app.url).textFieldStyle(.roundedBorder)
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text("02  ELIGE EL RESULTADO").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    modeButton("prompt", "Preparar prompt", "Para copiarlo a tu IA favorita", "doc.on.clipboard")
                    modeButton("api", "Generar informe", "Directamente con Muse Spark", "sparkles")
                }
                DisclosureGroup("Opciones", isExpanded: $app.options) {
                    VStack(alignment: .leading, spacing: 10) {
                        if app.mode == "api" {
                            SecureField("Clave API de Meta", text: $app.key).textFieldStyle(.roundedBorder)
                            HStack {
                                Text("La clave se guarda en el Llavero de macOS.").font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Button("Olvidar", action: app.forgetKey).controlSize(.small).disabled(app.key.isEmpty || app.busy)
                            }
                            TextField("Modelo", text: $app.model).textFieldStyle(.roundedBorder)
                        }
                        Picker("Sesión de YouTube", selection: $app.browser) {
                            Text("Sin navegador").tag("")
                            Text("Chrome").tag("chrome")
                            Text("Firefox").tag("firefox")
                            Text("Safari").tag("safari")
                        }
                        Text("Opcional: usa la sesión de tu navegador si YouTube pide iniciar sesión.").font(.caption).foregroundStyle(.secondary)
                        Text("O pega una transcripción").font(.caption.weight(.medium))
                        TextEditor(text: $app.transcript).font(.system(size: 11)).frame(height: 85)
                            .overlay(RoundedRectangle(cornerRadius: 5).stroke(.quaternary))
                    }.padding(.top, 10)
                }
                Spacer(minLength: 0)
                if app.busy {
                    HStack { ProgressView().controlSize(.small); Text("Trabajando…").font(.subheadline); Spacer(); Button("Cancelar", action: app.cancel) }
                } else {
                    Button(action: app.start) {
                        HStack { Text(app.mode == "prompt" ? "Preparar prompt" : "Generar informe"); Spacer(); Image(systemName: "arrow.right") }.padding(.vertical, 7)
                    }.buttonStyle(.borderedProminent).tint(accent).keyboardShortcut(.return, modifiers: .command)
                }
                Text("En tu Mac · Sin cuentas ni suscripciones\nEl modo API consume tu saldo de Meta.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(26)
            }.frame(width: 315).background(Color(nsColor: .controlBackgroundColor))
            Divider()
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Text(app.output.isEmpty ? "TU ESPACIO DE LECTURA" : "RESULTADO").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Spacer()
                    Button(action: app.copy) { Label("Copiar", systemImage: "doc.on.doc") }.disabled(app.output.isEmpty)
                    Button(action: app.save) { Label("Guardar", systemImage: "square.and.arrow.down") }.disabled(app.output.isEmpty)
                }
                if app.output.isEmpty {
                    Spacer()
                    Image(systemName: "text.alignleft").font(.system(size: 42, weight: .light)).foregroundStyle(accent)
                    Text("Tu próxima idea\nempieza aquí.").font(.system(size: 37, weight: .semibold, design: .serif))
                    Text("Convierte un vídeo largo en un esquema claro,\nuna tesis principal y sus momentos más valiosos.")
                        .font(.system(size: 15)).foregroundStyle(.secondary).lineSpacing(5)
                    HStack(spacing: 16) { Label("Esquema", systemImage: "list.bullet"); Label("Tesis", systemImage: "lightbulb"); Label("Highlights", systemImage: "bookmark") }
                        .font(.caption).foregroundStyle(accent).padding(.top, 12)
                    Spacer()
                } else {
                    Text(app.title).font(.title2.weight(.semibold)).lineLimit(2)
                    TextEditor(text: $app.output).font(.system(size: 14, design: .monospaced)).lineSpacing(5)
                }
                if !app.status.isEmpty {
                    HStack(alignment: .top) {
                        Image(systemName: app.error ? "exclamationmark.circle" : "info.circle")
                        Text(app.status).textSelection(.enabled)
                    }.font(.callout).foregroundStyle(app.error ? Color.red : Color.secondary)
                        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background((app.error ? Color.red : accent).opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
                }
            }.padding(32).frame(maxWidth: .infinity, maxHeight: .infinity).background(Color(nsColor: .textBackgroundColor))
        }.frame(minWidth: 950, minHeight: 700)
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in app.cancel() }
    }
    func modeButton(_ value: String, _ title: String, _ subtitle: String, _ icon: String) -> some View {
        Button { app.mode = value; if value == "api" { app.loadKey() } } label: {
            HStack(spacing: 12) {
                Image(systemName: icon).font(.title3).frame(width: 23)
                VStack(alignment: .leading, spacing: 4) { Text(title).font(.system(size: 14, weight: .semibold)); Text(subtitle).font(.caption).foregroundStyle(.secondary) }
                Spacer()
                if app.mode == value { Image(systemName: "checkmark.circle.fill") }
            }.padding(13).frame(maxWidth: .infinity, alignment: .leading)
                .background(app.mode == value ? accent.opacity(0.10) : Color.clear, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(app.mode == value ? accent.opacity(0.5) : Color.gray.opacity(0.2)))
        }.buttonStyle(.plain).foregroundStyle(app.mode == value ? accent : .primary).disabled(app.busy)
    }
}

@main
struct ReZmeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        Window("reZme", id: "main") { ContentView() }
            .defaultSize(width: 1050, height: 740)
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
