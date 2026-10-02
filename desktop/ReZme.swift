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

enum Pane: Hashable { case queue, library, contrast, cleanup, agents, report, settings }

/// Uso que un agente ha anotado: qué afirmaciones empleó y para qué.
struct AgentUse: Identifiable {
    let id: Int
    let at: String
    let agent: String
    let purpose: String
    let claims: Int
}

/// Dos afirmaciones de canales distintos y cómo se relacionan.
struct CrossPair: Identifiable {
    let id: Int
    let relation: String
    let entity: String
    let reason: String
    let aText: String
    let aFrom: String
    let bText: String
    let bFrom: String
}

/// Relación de una afirmación con otra de otra fuente, ya redactada.
struct CrossNote: Identifiable {
    let id = UUID()
    let kind: String
    let text: String
}

/// Propuesta de fusionar una entidad (`drop`) dentro de otra (`keep`).
struct MergeProposal: Identifiable {
    let id: Int
    let origin: String
    let reason: String
    let sameType: Bool
    let keepName: String
    let keepType: String
    let keepClaims: Int
    let dropName: String
    let dropType: String
    let dropClaims: Int
}

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
    /// Trabajo de extracción pendiente y su consumo estimado (nil si ya tiene afirmaciones).
    var job: Int? = nil
    var calls = 0
    var tokensIn = 0
    var tokensOut = 0
}

/// Un paso del mecanismo o una condición, y si lo dice el autor o lo deduce el modelo.
struct KnowledgeItem: Identifiable {
    let id = UUID()
    let text: String
    let stated: Bool
}

struct ClaimItem: Identifiable {
    let id: Int
    let verified: Bool
    let statement: String
    let kind: String
    let entity: String
    let metric: String
    let time: String
    let link: String
    let quote: String
    let reasons: [String]
    let implications: [String]
    var title = ""
    var mechanism: [KnowledgeItem] = []
    var appliesWhen: [KnowledgeItem] = []
    var failsWhen: [KnowledgeItem] = []
    var tags: [String] = []
    var relations: [String] = []
    var supportedBy = 0
    var contradictedBy = 0
    var cross: [CrossNote] = []
}

struct SourceDetail {
    let id: Int
    let title: String
    let url: String
    let claims: [ClaimItem]
    var verified: [ClaimItem] { claims.filter { $0.verified } }
    var unverified: [ClaimItem] { claims.filter { !$0.verified } }
}

struct ClaimHit: Identifiable {
    let id: Int
    let statement: String
    let meta: String
    var supportedBy = 0
    var contradictedBy = 0
}

/// Fusión ya aplicada, por si hay que deshacerla.
struct DoneMerge: Identifiable {
    let id: Int
    let drop: String
    let keep: String
    let claims: Int
    let undoable: Bool
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
    // Agentes
    @Published var agentConfig = ""
    @Published var agentTools: [String] = []
    @Published var agentUses: [AgentUse] = []
    @Published var agentQuestion = ""
    @Published var agentAnswer = ""
    @Published var agentAnswerNote = ""

    // Contraste entre vídeos
    @Published var crossStats: [String: Int] = [:]
    @Published var crossPairs: [CrossPair] = []
    @Published var crossFilter = "contradicts"
    /// Fecha «como se sabía el…» para el buscador (AAAA-MM-DD), o vacía para hoy.
    @Published var knownAt = ""
    var shownPairs: [CrossPair] { crossPairs.filter { $0.relation == crossFilter } }

    // Limpieza del catálogo
    @Published var proposals: [MergeProposal] = []
    @Published var catalogStats: [String: Int] = [:]
    @Published var doneMerges: [DoneMerge] = []
    @Published var showDoneMerges = false
    /// Propuestas que no se fusionarán: las desmarcadas a mano y, de entrada, las de tipos distintos.
    @Published var unchecked: Set<Int> = []
    @Published var useModelForCleanup = true
    @Published var confirmMerge = false
    private var knownProposals: Set<Int> = []
    var chosenProposals: [MergeProposal] { proposals.filter { !unchecked.contains($0.id) } }

    /// Vídeo abierto en detalle, con sus afirmaciones.
    @Published var detail: SourceDetail? = nil

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
    /// Modelo que extrae las afirmaciones: "meta" (Muse Spark) o "claude-code".
    @Published var engine: String { didSet { defaults.set(engine, forKey: "engine") } }
    @Published var confirmExtract = false
    /// Velocidad de la extracción: cuánto razona el modelo ("low", "medium" o "" = el suyo) y
    /// cuántos tramos se le envían a la vez.
    @Published var reasoning: String { didSet { defaults.set(reasoning, forKey: "reasoning") } }
    @Published var workers: Int { didSet { defaults.set(workers, forKey: "workers") } }
    /// Presupuesto de extracción: tope por tanda y precios por millón de tokens, en dólares.
    @Published var budget: String { didSet { defaults.set(budget, forKey: "budget") } }
    @Published var priceIn: String { didSet { defaults.set(priceIn, forKey: "priceIn") } }
    @Published var priceOut: String { didSet { defaults.set(priceOut, forKey: "priceOut") } }
    /// Trabajos que el usuario ha dejado fuera de la extracción.
    @Published var excluded: Set<Int> = []
    @Published var spent: Double = 0
    @Published var pilot = false

    private var process: Process?
    private var generation = UUID()
    private var savedKey = ""
    private var keyLoaded = false

    init() {
        model = defaults.string(forKey: "model") ?? "muse-spark-1.3"
        browser = defaults.string(forKey: "browser") ?? ""
        whisper = defaults.bool(forKey: "whisper")
        engine = defaults.string(forKey: "engine") ?? "meta"
        budget = defaults.string(forKey: "budget") ?? "5"
        reasoning = defaults.string(forKey: "reasoning") ?? "low"
        workers = defaults.object(forKey: "workers") as? Int ?? 2
        priceIn = defaults.string(forKey: "priceIn") ?? ""
        priceOut = defaults.string(forKey: "priceOut") ?? ""
    }

    var root: URL { Bundle.main.bundleURL.resolvingSymlinksInPath().deletingLastPathComponent() }
    var database: URL { root.appendingPathComponent("rezme_data/rezme.db") }
    var waiting: Int { (counts["pending"] ?? 0) + (counts["running"] ?? 0) }
    var saved: Int { (counts["saved"] ?? 0) + (counts["done"] ?? 0) }
    var toExtract: Int { stats["to_extract"] ?? 0 }
    var calibrated: Bool { (stats["calibrated"] ?? 0) > 0 }

    private func number(_ text: String) -> Double {
        max(0, Double(text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")) ?? 0)
    }
    var budgetValue: Double { number(budget) }
    var hasPrices: Bool { number(priceIn) > 0 && number(priceOut) > 0 }
    func cost(_ source: LibrarySource) -> Double {
        (Double(source.tokensIn) * number(priceIn) + Double(source.tokensOut) * number(priceOut)) / 1_000_000
    }
    func money(_ value: Double) -> String {
        value > 0 && value < 0.01 ? "< 0,01 $" : String(format: "%.2f $", value).replacingOccurrences(of: ".", with: ",")
    }
    func tokens(_ value: Int) -> String {
        value >= 1_000_000 ? String(format: "%.2f M", Double(value) / 1_000_000).replacingOccurrences(of: ".", with: ",")
                           : "\(Int((Double(value) / 1000).rounded())) mil"
    }
    var pending: [LibrarySource] { sources.filter { $0.job != nil } }
    var selected: [LibrarySource] { pending.filter { !excluded.contains($0.job ?? -1) } }
    /// Lo que se va a extraer: la selección, o en una prueba solo el vídeo más barato de ella.
    var batchToRun: [LibrarySource] {
        pilot ? Array(selected.sorted { $0.calls == $1.calls ? $0.tokensIn < $1.tokensIn : $0.calls < $1.calls }.prefix(1)) : selected
    }
    func summary(_ items: [LibrarySource]) -> String {
        let calls = items.reduce(0) { $0 + $1.calls }
        let tin = items.reduce(0) { $0 + $1.tokensIn }, tout = items.reduce(0) { $0 + $1.tokensOut }
        var text = "\(calls) llamadas · ≈ \(tokens(tin)) tokens de entrada y \(tokens(tout)) de salida"
        if hasPrices { text += " · ≈ \(money(items.reduce(0) { $0 + cost($1) }))" }
        return text
    }
    /// Texto de la confirmación previa a extraer. Se monta por partes: una sola expresión larga
    /// de cadenas concatenadas agota el comprobador de tipos en compiladores anteriores.
    var confirmMessage: String {
        var text = ""
        if pilot, let first = batchToRun.first { text += "«\(first.title)»: " }
        text += summary(batchToRun) + ". "
        if !calibrated { text += "Es una estimación sin calibrar. " }
        text += "La extracción se detendrá sola al llegar a \(money(budgetValue)). "
        text += engine == "meta" ? "El consumo se factura en tu cuenta de Meta." : "Se usa tu suscripción de Claude Code."
        text += " Puedes pausar cuando quieras: continuará donde lo dejó."
        return text
    }
    var contrastHeadline: String {
        let pending = crossStats["pendientes"] ?? 0
        if busy == .contrast { return "Contrastando con \(engineName)…" }
        if pending == 0 { return "Todo lo compartido entre canales está contrastado." }
        return "\(pending) entidades por contrastar · unas \(crossStats["llamadas"] ?? 0) llamadas al modelo"
    }
    func proposalDetail(_ proposal: MergeProposal) -> String {
        var text = "\(proposal.dropType), \(proposal.dropClaims) afirmaciones"
        text += " → \(proposal.keepType), \(proposal.keepClaims) afirmaciones"
        if !proposal.reason.isEmpty { text += " · " + proposal.reason }
        return text
    }
    /// Etiqueta de la fila de un vídeo: coste estimado si falta extraer, o sus afirmaciones.
    func badge(_ source: LibrarySource) -> String {
        if source.job != nil {
            let calls = "\(source.calls) llamadas"
            return hasPrices ? "≈ \(money(cost(source))) · " + calls : calls
        }
        return source.verified > 0 ? "\(source.verified) afirmaciones" : "Sin afirmaciones"
    }
    func toggle(_ source: LibrarySource) {
        guard let job = source.job else { return }
        if excluded.contains(job) { excluded.remove(job) } else { excluded.insert(job) }
    }
    var engineName: String { engine == "meta" ? "Muse Spark" : "Claude Code" }

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
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "\(home)/.npm-global/bin:\(home)/.local/bin:\(home)/.claude/local:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
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
        if owner == .contrast {
            status = "Contraste en pausa. Lo ya contrastado se conserva."
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.cross("status") }
        } else if owner == .queue || owner == .library {
            status = owner == .queue ? "Cola en pausa. Lo ya guardado se conserva." : "Extracción en pausa. Continuará donde lo dejó."
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
               onEvent: { event in self.queueEvent(event) },
               onExit: { ok in
                   guard tracked else { return }
                   if ok && !self.error { self.queueText = "" }
                   else if !ok && !self.error { self.fail("El proceso se ha interrumpido. Vuelve a lanzarlo: continuará donde lo dejó.") }
                   self.loadLibrary()
               })
    }

    private func queueEvent(_ event: [String: Any]) {
        switch event["type"] as? String {
        case "progress", "done": status = event["message"] as? String ?? ""
        case "queue":
            counts = event["counts"] as? [String: Int] ?? [:]
            jobs = (event["jobs"] as? [[String: Any]] ?? []).compactMap { job in
                guard let id = job["id"] as? Int else { return nil }
                return QueueJob(id: id, title: job["title"] as? String ?? "",
                                state: job["state"] as? String ?? "", detail: job["detail"] as? String ?? "")
            }
        case "error": fail(event["message"] as? String ?? "No se pudo procesar la cola.")
        default: break
        }
    }

    // MARK: Extracción de afirmaciones

    /// Pide confirmación antes de gastar llamadas al modelo. Con `pilot`, solo el vídeo más barato.
    func askExtract(pilot: Bool) {
        guard busy == nil else { status = "Hay una tarea en curso. Espera a que termine o páusala."; error = true; return }
        if engine == "meta" {
            loadKey()
            if key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                pane = .settings; fail("Introduce tu clave de Meta en los ajustes, o elige Claude Code como motor."); return
            }
            if !hasPrices {
                pane = .settings; fail("Introduce en los ajustes el precio por millón de tokens de tu modelo: sin él no se puede aplicar el tope de gasto."); return
            }
        }
        if budgetValue <= 0 { pane = .settings; fail("Fija en los ajustes un tope de gasto por tanda."); return }
        self.pilot = pilot
        guard !batchToRun.isEmpty else { fail("No hay ningún vídeo seleccionado."); return }
        confirmExtract = true
    }

    func extract() {
        guard busy == nil else { return }
        error = false
        if engine == "meta" { saveKey() }
        let ids = batchToRun.compactMap { $0.job }.map(String.init).joined(separator: ",")
        guard !ids.isEmpty else { return }
        spent = 0
        let started = launch(["mode": "queue", "action": "extract", "engine": engine, "db": database.path,
                              "key": engine == "meta" ? key.trimmingCharacters(in: .whitespacesAndNewlines) : "", "model": model,
                              "ids": ids, "budget": String(budgetValue), "price_in": String(number(priceIn)), "price_out": String(number(priceOut)),
                              "reasoning": reasoning, "workers": String(workers)],
                             owner: .library,
                             onEvent: { event in
                                 self.queueEvent(event)
                                 switch event["type"] as? String {
                                 case "queue": self.loadLibrary()
                                 case "spend": self.spent = event["cost"] as? Double ?? self.spent
                                 default: break
                                 }
                             },
                             onExit: { ok in
                                 if !ok && !self.error { self.fail("La extracción se ha interrumpido. Vuelve a lanzarla: continuará donde lo dejó.") }
                                 self.queue("status"); self.loadLibrary()
                             })
        if started { status = "Preparando la extracción con \(engineName)…" }
    }

    // MARK: Base de conocimiento

    func open(_ source: LibrarySource) {
        detail = SourceDetail(id: source.id, title: source.title, url: source.url, claims: [])
        loadLibrary()
    }
    /// Devuelve el vídeo abierto a la lista de extracción, para rehacerlo con el prompt actual.
    func reextract() {
        guard busy == nil, let detail else { return }
        let started = launch(["mode": "queue", "action": "reextract", "db": database.path, "source": String(detail.id)],
                             owner: nil, onEvent: { event in self.queueEvent(event) },
                             onExit: { ok in
                                 guard ok else { return }
                                 self.detail = nil; self.excluded = []
                                 self.status = "Vídeo devuelto a la lista: selecciónalo y pulsa «Extraer selección»."; self.error = false
                                 self.loadLibrary()
                             })
        if !started { fail("No se pudo preparar la reextracción.") }
    }
    func openLink(_ link: String) {
        if let url = URL(string: link), !link.isEmpty { NSWorkspace.shared.open(url) }
    }

    func loadLibrary(search: Bool = false) {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { hits = nil }
        launch(["mode": "library", "db": database.path, "query": search ? text : "",
                "known_at": knownAt.trimmingCharacters(in: .whitespaces),
                "source": detail.map { String($0.id) } ?? ""], owner: nil, onEvent: { event in
            switch event["type"] as? String {
            case "detail":
                guard let id = event["id"] as? Int, id == self.detail?.id else { break }
                let claims: [ClaimItem] = (event["claims"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let claimID = item["id"] as? Int else { return nil }
                    let knowledge: (String) -> [KnowledgeItem] = { name in
                        (item[name] as? [[String: Any]] ?? []).map {
                            KnowledgeItem(text: $0["text"] as? String ?? "", stated: $0["stated"] as? Bool ?? false)
                        }
                    }
                    return ClaimItem(id: claimID, verified: item["verified"] as? Bool ?? false,
                                     statement: item["statement"] as? String ?? "", kind: item["kind"] as? String ?? "",
                                     entity: item["entity"] as? String ?? "", metric: item["metric"] as? String ?? "",
                                     time: item["time"] as? String ?? "", link: item["link"] as? String ?? "",
                                     quote: item["quote"] as? String ?? "", reasons: item["reasons"] as? [String] ?? [],
                                     implications: item["implications"] as? [String] ?? [],
                                     title: item["title"] as? String ?? "", mechanism: knowledge("mechanism"),
                                     appliesWhen: knowledge("applies_when"), failsWhen: knowledge("fails_when"),
                                     tags: item["tags"] as? [String] ?? [], relations: item["relations"] as? [String] ?? [],
                                     supportedBy: item["supported_by"] as? Int ?? 0, contradictedBy: item["contradicted_by"] as? Int ?? 0,
                                     cross: (item["cross"] as? [[String: Any]] ?? []).map {
                                         CrossNote(kind: $0["kind"] as? String ?? "", text: $0["text"] as? String ?? "")
                                     })
                }
                self.detail = SourceDetail(id: id, title: event["title"] as? String ?? "", url: event["url"] as? String ?? "", claims: claims)
            case "library":
                self.stats = event["stats"] as? [String: Int] ?? [:]
                self.sources = (event["sources"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int else { return nil }
                    let parts = [item["channel"] as? String ?? "", item["date"] as? String ?? ""].filter { !$0.isEmpty }
                    return LibrarySource(id: id, title: item["title"] as? String ?? "", subtitle: parts.joined(separator: " · "),
                                         detail: item["detail"] as? String ?? "", verified: item["verified"] as? Int ?? 0,
                                         url: item["url"] as? String ?? "", job: item["job"] as? Int,
                                         calls: item["calls"] as? Int ?? 0, tokensIn: item["tokens_in"] as? Int ?? 0,
                                         tokensOut: item["tokens_out"] as? Int ?? 0)
                }
            case "hits":
                self.hits = (event["items"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int else { return nil }
                    return ClaimHit(id: id, statement: item["statement"] as? String ?? "", meta: item["meta"] as? String ?? "",
                                    supportedBy: item["supported_by"] as? Int ?? 0, contradictedBy: item["contradicted_by"] as? Int ?? 0)
                }
            case "error": self.fail(event["message"] as? String ?? "No se pudo consultar la base.")
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

    // MARK: Agentes

    /// `status` trae la configuración para conectar agentes y lo que han usado; `ask` muestra qué
    /// recibiría un agente para una pregunta. No llama a ningún modelo: solo lee la base.
    func agents(_ action: String) {
        var payload = ["mode": "agents", "action": action, "db": database.path, "root": root.path]
        if action == "ask" {
            payload["question"] = agentQuestion
            payload["known_at"] = knownAt.trimmingCharacters(in: .whitespaces)
            agentAnswerNote = "Buscando…"
        }
        launch(payload, owner: nil, onEvent: { event in
            switch event["type"] as? String {
            case "agents":
                self.agentConfig = event["config"] as? String ?? ""
                self.agentTools = (event["tools"] as? [[String: Any]] ?? []).compactMap { $0["name"] as? String }
                self.agentUses = (event["uses"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int else { return nil }
                    return AgentUse(id: id, at: item["at"] as? String ?? "", agent: item["agent"] as? String ?? "",
                                    purpose: item["purpose"] as? String ?? "", claims: item["claims"] as? Int ?? 0)
                }
            case "agents_answer":
                self.agentAnswer = event["text"] as? String ?? ""
                let omitted = event["omitted"] as? Int ?? 0
                self.agentAnswerNote = "\(event["claims"] as? Int ?? 0) afirmaciones · unos \(event["tokens"] as? Int ?? 0) tokens"
                    + (omitted > 0 ? " · \(omitted) más no caben" : "")
            case "error":
                self.agentAnswerNote = ""
                self.fail(event["message"] as? String ?? "No se pudo consultar la base.")
            default: break
            }
        })
    }
    func copyAgentConfig() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(agentConfig, forType: .string)
        status = "Configuración copiada. Pégala en la configuración de servidores MCP de tu agente."; error = false
    }

    // MARK: Contraste entre vídeos

    /// `run` cruza con el modelo las afirmaciones de las entidades que comparten varios canales.
    func cross(_ action: String) {
        let tracked = action == "run"
        var payload = ["mode": "cross", "action": action, "db": database.path]
        if tracked {
            guard busy == nil else { status = "Hay una tarea en curso. Espera a que termine o páusala."; error = true; return }
            error = false
            if engine == "meta" {
                loadKey()
                if key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    pane = .settings; fail("Introduce tu clave de Meta en los ajustes."); return
                }
            }
            payload["engine"] = engine
            payload["key"] = engine == "meta" ? key.trimmingCharacters(in: .whitespacesAndNewlines) : ""
            payload["model"] = model
            payload["budget"] = String(budgetValue)
            payload["price_in"] = String(number(priceIn))
            payload["price_out"] = String(number(priceOut))
            payload["reasoning"] = reasoning
            payload["workers"] = String(workers)
        }
        let started = launch(payload, owner: tracked ? .contrast : nil, onEvent: { event in
            switch event["type"] as? String {
            case "progress", "done": self.status = event["message"] as? String ?? ""
            case "error": self.fail(event["message"] as? String ?? "No se pudo completar el contraste.")
            case "cross":
                self.crossStats = event["stats"] as? [String: Int] ?? [:]
                self.crossPairs = (event["pairs"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int, let a = item["a"] as? [String: Any], let b = item["b"] as? [String: Any] else { return nil }
                    let origin: ([String: Any]) -> String = { side in
                        [side["channel"] as? String ?? "", side["title"] as? String ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
                    }
                    return CrossPair(id: id, relation: item["relation"] as? String ?? "", entity: item["entity"] as? String ?? "",
                                     reason: item["reason"] as? String ?? "", aText: a["text"] as? String ?? "", aFrom: origin(a),
                                     bText: b["text"] as? String ?? "", bFrom: origin(b))
                }
            default: break
            }
        }, onExit: { ok in
            if tracked && !ok && !self.error { self.fail("El contraste se ha interrumpido. Vuelve a lanzarlo: continuará con lo que falte.") }
            if tracked { self.loadLibrary() }
        })
        if started && tracked { status = "Contrastando lo que dicen los distintos canales…" }
    }

    // MARK: Limpieza del catálogo

    /// `propose` busca duplicados, `apply` fusiona las elegidas, `dismiss` las descarta y `dedupe`
    /// retira afirmaciones repetidas. Ninguna fusión se aplica sin pasar por `apply`.
    func undoMerge(_ merge: DoneMerge) { catalog("undo", ids: String(merge.id)) }

    func catalog(_ action: String, ids: String? = nil) {
        let tracked = action == "propose" || action == "apply" || action == "dedupe" || action == "undo"
        if tracked {
            guard busy == nil else { status = "Hay una tarea en curso. Espera a que termine o páusala."; error = true; return }
            error = false
        }
        var payload = ["mode": "catalog", "action": action, "db": database.path]
        if action == "apply" || action == "dismiss" {
            payload["ids"] = chosenProposals.map { String($0.id) }.joined(separator: ",")
        }
        if let ids { payload["ids"] = ids }
        if action == "propose" && useModelForCleanup {
            if engine == "meta" {
                loadKey()
                if key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    pane = .settings; fail("Introduce tu clave de Meta en los ajustes, o desmarca el uso del modelo."); return
                }
            }
            payload["use_model"] = "1"
            payload["engine"] = engine
            payload["key"] = engine == "meta" ? key.trimmingCharacters(in: .whitespacesAndNewlines) : ""
            payload["model"] = model
            payload["budget"] = String(budgetValue)
            payload["price_in"] = String(number(priceIn))
            payload["price_out"] = String(number(priceOut))
            payload["reasoning"] = reasoning
        }
        let started = launch(payload, owner: tracked ? .cleanup : nil, onEvent: { event in
            switch event["type"] as? String {
            case "progress", "done": self.status = event["message"] as? String ?? ""
            case "error": self.fail(event["message"] as? String ?? "No se pudo completar la limpieza.")
            case "catalog":
                self.catalogStats = ["entities": event["entities"] as? Int ?? 0, "merged": event["merged"] as? Int ?? 0,
                                     "duplicates": event["duplicates"] as? Int ?? 0]
                let items: [MergeProposal] = (event["proposals"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int, let keep = item["keep"] as? [String: Any],
                          let drop = item["drop"] as? [String: Any] else { return nil }
                    return MergeProposal(id: id, origin: item["origin"] as? String ?? "", reason: item["reason"] as? String ?? "",
                                         sameType: item["same_type"] as? Bool ?? true,
                                         keepName: keep["name"] as? String ?? "", keepType: keep["type"] as? String ?? "",
                                         keepClaims: keep["claims"] as? Int ?? 0, dropName: drop["name"] as? String ?? "",
                                         dropType: drop["type"] as? String ?? "", dropClaims: drop["claims"] as? Int ?? 0)
                }
                // Las de tipos distintos son las más dudosas: llegan desmarcadas.
                for item in items where !self.knownProposals.contains(item.id) && !item.sameType { self.unchecked.insert(item.id) }
                self.knownProposals.formUnion(items.map { $0.id })
                self.proposals = items
                self.doneMerges = (event["merges"] as? [[String: Any]] ?? []).compactMap { item in
                    guard let id = item["id"] as? Int else { return nil }
                    return DoneMerge(id: id, drop: item["drop"] as? String ?? "", keep: item["keep"] as? String ?? "",
                                     claims: item["claims"] as? Int ?? 0, undoable: item["undoable"] as? Bool ?? false)
                }
            default: break
            }
        }, onExit: { ok in
            if tracked && !ok && !self.error { self.fail("La limpieza se ha interrumpido. Puedes volver a lanzarla.") }
            if tracked { self.loadLibrary() }
        })
        if started && action == "propose" { status = "Buscando entidades duplicadas…" }
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
                    case .agents: agentsPane
                    case .contrast: contrastPane
                    case .cleanup: cleanupPane
                    case .settings: settingsPane
                    }
                }
                .padding(.horizontal, 34).padding(.top, 30).padding(.bottom, 18)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                if !app.status.isEmpty { statusBar }
            }.background(Color(nsColor: .textBackgroundColor))
        }
        .frame(minWidth: 980, minHeight: 700)
        .onAppear { app.queue("status"); app.loadLibrary(); app.catalog("status"); app.cross("status") }
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
            navItem(.contrast, "Contraste", "arrow.left.arrow.right", badge: 0)
            navItem(.cleanup, "Limpieza", "wand.and.stars", badge: app.proposals.count)
            navItem(.agents, "Agentes", "cpu", badge: 0)
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
            if pane == .library { if app.pane == .library { app.detail = nil }; app.loadLibrary(search: !app.query.isEmpty) }
            if pane == .queue && app.busy == nil { app.queue("status") }
            if pane == .settings { app.loadKey() }
            if pane == .cleanup && app.busy == nil { app.catalog("status") }
            if pane == .contrast && app.busy != .contrast { app.cross("status") }
            if pane == .agents { app.agents("status") }
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
                    tile(app.saved, "Guardados", accent)
                    tile(app.waiting, "En cola")
                    tile(app.counts["failed"] ?? 0, "Fallidos", .red)
                    tile(app.counts["skipped"] ?? 0, "Sin subtítulos", .orange)
                }.padding(.bottom, 14)
                HStack {
                    Text("VÍDEOS").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Spacer()
                    Button { app.queue("retry") } label: { Label("Reintentar", systemImage: "arrow.clockwise") }
                        .disabled(app.busy != nil || (app.counts["failed"] ?? 0) + (app.counts["skipped"] ?? 0) == 0)
                    Button { app.queue("clear") } label: { Label("Quitar terminados", systemImage: "checkmark.circle") }
                        .disabled(app.busy != nil || (app.counts["done"] ?? 0) == 0)
                }.controlSize(.small).padding(.bottom, 6)
                rows(app.jobs) { job in
                    HStack(alignment: .top, spacing: 10) {
                        Group {
                            switch job.state {
                            case "done": Image(systemName: "checkmark.seal.fill").foregroundStyle(accent)
                            case "saved": Image(systemName: "checkmark.circle").foregroundStyle(accent)
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

    /// Cuántos canales independientes sostienen o contradicen la afirmación.
    @ViewBuilder func corroboration(_ supported: Int, _ contradicted: Int) -> some View {
        if supported > 0 || contradicted > 0 {
            HStack(spacing: 10) {
                if supported > 0 {
                    Label(supported == 1 ? "Lo sostiene otro canal" : "Lo sostienen otros \(supported) canales", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(accent)
                }
                if contradicted > 0 {
                    Label(contradicted == 1 ? "Lo contradice otro canal" : "Lo contradicen otros \(contradicted) canales", systemImage: "exclamationmark.circle.fill")
                        .foregroundStyle(Color.orange)
                }
            }.font(.caption.weight(.medium))
        }
    }

    /// Bloque «Por qué / Aplica cuando / Falla cuando». La comilla marca lo que dice el autor.
    @ViewBuilder func knowledgeBlock(_ title: String, _ items: [KnowledgeItem]) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(items) { item in
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: item.stated ? "quote.opening" : "sparkle").font(.caption2)
                            .foregroundStyle(item.stated ? accent : Color.secondary).frame(width: 12).padding(.top, 2)
                            .help(item.stated ? "Lo dice el autor" : "Deducido por el modelo")
                        Text(item.text).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }.padding(.top, 2)
        }
    }

    func claimRow(_ claim: ClaimItem) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            if !claim.title.isEmpty { Text(claim.title).font(.system(size: 14, weight: .semibold)) }
            Text(claim.statement).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Text(claim.kind).font(.caption.weight(.medium)).foregroundStyle(accent)
                    .padding(.horizontal, 7).padding(.vertical, 2).background(accent.opacity(0.10), in: Capsule())
                if !claim.metric.isEmpty {
                    Text(claim.metric).font(.caption.weight(.medium)).padding(.horizontal, 7).padding(.vertical, 2).background(panel, in: Capsule())
                }
                if !claim.entity.isEmpty { Text(claim.entity).font(.caption).foregroundStyle(.secondary) }
                if !claim.time.isEmpty {
                    Button { app.openLink(claim.link) } label: { Label(claim.time, systemImage: "play.circle") }
                        .buttonStyle(.plain).font(.caption).foregroundStyle(accent).help("Abrir el vídeo en ese momento")
                }
            }
            if !claim.quote.isEmpty {
                Text("«\(claim.quote)»").font(.caption).italic().foregroundStyle(.secondary).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            knowledgeBlock("POR QUÉ", claim.mechanism)
            knowledgeBlock("APLICA CUANDO", claim.appliesWhen)
            knowledgeBlock("FALLA CUANDO", claim.failsWhen)
            if !claim.tags.isEmpty {
                Text("Sirve para: " + claim.tags.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
            }
            ForEach(claim.implications, id: \.self) { implication in
                Label(implication, systemImage: "arrow.turn.down.right").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(claim.relations, id: \.self) { relation in
                Label(relation, systemImage: "link").font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            corroboration(claim.supportedBy, claim.contradictedBy)
            ForEach(claim.cross) { note in
                Label(note.text, systemImage: note.kind == "contradicts" ? "arrow.left.arrow.right" : "checkmark")
                    .font(.caption).foregroundStyle(note.kind == "contradicts" ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(claim.reasons, id: \.self) { reason in
                Label("No verificada: \(reason)", systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Color.orange)
            }
        }
    }

    func detailPane(_ detail: SourceDetail) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { app.detail = nil } label: { Label("Base de conocimiento", systemImage: "chevron.left") }
                .buttonStyle(.plain).foregroundStyle(accent).padding(.bottom, 10)
            HStack(alignment: .top) {
                Text(detail.title).font(.system(size: 22, weight: .semibold, design: .serif)).lineLimit(2)
                Spacer()
                if !detail.claims.isEmpty {
                    Button { app.reextract() } label: { Label("Volver a extraer", systemImage: "arrow.clockwise") }
                        .disabled(app.busy != nil).help("Rehace la extracción de este vídeo con la versión actual. La anterior se conserva como histórico.")
                }
                if !detail.url.isEmpty { Button { app.openLink(detail.url) } label: { Label("Ver en YouTube", systemImage: "play.rectangle") } }
            }.padding(.bottom, 4)
            Text(detail.claims.isEmpty ? "Aún no se han extraído afirmaciones de este vídeo."
                 : "\(detail.verified.count) afirmaciones verificadas · \(detail.unverified.count) sin verificar")
                .font(.callout).foregroundStyle(.secondary).padding(.bottom, 6)
            if !detail.claims.isEmpty {
                HStack(spacing: 14) {
                    Label("lo dice el autor", systemImage: "quote.opening").foregroundStyle(accent)
                    Label("deducido por el modelo", systemImage: "sparkle").foregroundStyle(.secondary)
                }.font(.caption).padding(.bottom, 12)
            }
            if detail.claims.isEmpty {
                emptyState("text.badge.checkmark", "Sin afirmaciones todavía", "Vuelve a la base, selecciona este vídeo\ny pulsa «Extraer selección».")
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        if !detail.verified.isEmpty {
                            Text("VERIFICADAS · el autor lo dice y la cita y la cifra están en la transcripción")
                                .font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.bottom, 4)
                            ForEach(detail.verified) { claim in
                                claimRow(claim).padding(.vertical, 10).frame(maxWidth: .infinity, alignment: .leading)
                                Divider().opacity(0.6)
                            }
                        }
                        if !detail.unverified.isEmpty {
                            Text("SIN VERIFICAR · el modelo las propuso, pero la cita o la cifra no se encontraron tal cual")
                                .font(.caption.weight(.semibold)).foregroundStyle(Color.orange).padding(.top, 18).padding(.bottom, 4)
                            ForEach(detail.unverified) { claim in
                                claimRow(claim).padding(.vertical, 10).frame(maxWidth: .infinity, alignment: .leading)
                                Divider().opacity(0.6)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder var libraryPane: some View {
        if let detail = app.detail { detailPane(detail) } else { libraryList }
    }

    var libraryList: some View {
        VStack(alignment: .leading, spacing: 0) {
            header("Base de conocimiento", "Lo que reZme ha guardado y verificado, listo para consultar.")
            HStack(spacing: 10) {
                tile(app.stats["videos"] ?? 0, "Vídeos con transcripción", accent)
                tile(app.stats["verified"] ?? 0, "Afirmaciones verificadas", accent)
                tile(app.stats["ungrounded"] ?? 0, "Sin verificar", .orange)
                tile(app.stats["entities"] ?? 0, "Entidades")
            }.padding(.bottom, 14)
            if app.busy == .library || app.toExtract > 0 {
                HStack(spacing: 12) {
                    Image(systemName: "sparkles").font(.title3).foregroundStyle(accent)
                    VStack(alignment: .leading, spacing: 3) {
                        if app.busy == .library {
                            Text("Extrayendo afirmaciones con \(app.engineName)…").font(.system(size: 13, weight: .semibold))
                            Text("Gastado \(app.money(app.spent)) de un tope de \(app.money(app.budgetValue)). Puedes pausar y continuar después.")
                                .font(.caption).foregroundStyle(.secondary)
                        } else {
                            Text("\(app.selected.count) de \(app.toExtract) vídeos sin afirmaciones seleccionados").font(.system(size: 13, weight: .semibold))
                            Text(app.summary(app.selected)).font(.caption).foregroundStyle(.secondary)
                            Text(app.calibrated ? "Estimación ajustada con el consumo real de las extracciones ya hechas. Tope por tanda: \(app.money(app.budgetValue))."
                                 : "Estimación sin calibrar: la salida del modelo aún no se ha medido. Prueba primero con un vídeo.")
                                .font(.caption).foregroundStyle(app.calibrated ? Color.secondary : Color.orange)
                        }
                    }
                    Spacer()
                    if app.busy == .library {
                        ProgressView().controlSize(.small)
                        Button("Pausar", action: app.cancel)
                    } else {
                        Button("Probar con un vídeo") { app.askExtract(pilot: true) }.disabled(app.busy != nil || app.selected.isEmpty)
                        Button { app.askExtract(pilot: false) } label: { Label("Extraer selección", systemImage: "arrow.right").padding(.horizontal, 4) }
                            .buttonStyle(.borderedProminent).tint(accent).disabled(app.busy != nil || app.selected.isEmpty)
                    }
                }
                .padding(13).background(accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
                .padding(.bottom, 14)
            }
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Buscar en las afirmaciones verificadas", text: $app.query).textFieldStyle(.plain)
                    .onSubmit { app.loadLibrary(search: true) }
                TextField("a fecha de AAAA-MM-DD", text: $app.knownAt).textFieldStyle(.plain).frame(width: 170)
                    .font(.callout).foregroundStyle(.secondary).onSubmit { app.loadLibrary(search: true) }
                    .help("Opcional: muestra solo lo que ya se sabía ese día y que entonces seguía vigente.")
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
                            corroboration(hit.supportedBy, hit.contradictedBy)
                        }
                    }
                }
            } else if app.sources.isEmpty {
                emptyState("cylinder.split.1x2", "Aún no hay nada guardado", "Añade vídeos desde la cola y aparecerán aquí\ncon su transcripción.")
            } else {
                HStack {
                    Text("VÍDEOS GUARDADOS").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Spacer()
                    if !app.pending.isEmpty {
                        Button("Todos") { app.excluded = [] }
                        Button("Ninguno") { app.excluded = Set(app.pending.compactMap { $0.job }) }
                    }
                }.controlSize(.small).disabled(app.busy == .library).padding(.bottom, 6)
                rows(app.sources) { source in
                    HStack(alignment: .top, spacing: 10) {
                        if let job = source.job {
                            Button { app.toggle(source) } label: {
                                Image(systemName: app.excluded.contains(job) ? "square" : "checkmark.square.fill")
                                    .foregroundStyle(app.excluded.contains(job) ? Color.secondary : accent)
                            }.buttonStyle(.plain).disabled(app.busy == .library).padding(.top, 1)
                        } else if !app.pending.isEmpty {
                            Image(systemName: "checkmark.seal.fill").foregroundStyle(accent).padding(.top, 1)
                        }
                        Button { app.open(source) } label: {
                            HStack(alignment: .top) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(source.title).lineLimit(1)
                                    Text([source.subtitle, source.detail].filter { !$0.isEmpty }.joined(separator: " · "))
                                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer()
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain).help("Ver las afirmaciones de este vídeo")
                        Text(app.badge(source))
                            .font(.caption).foregroundStyle(source.verified > 0 ? accent : Color.secondary)
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background((source.verified > 0 ? accent : Color.gray).opacity(0.12), in: Capsule())
                    }
                }
            }
            HStack(spacing: 8) {
                Image(systemName: "internaldrive").foregroundStyle(.secondary)
                Text(app.database.path).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("Mostrar en Finder", action: app.revealDatabase).controlSize(.small)
            }.padding(.top, 10)
        }
        .confirmationDialog(app.pilot ? "¿Probar la extracción con un vídeo?" : "¿Extraer las afirmaciones de \(app.batchToRun.count) vídeos?",
                            isPresented: $app.confirmExtract) {
            Button("Extraer con \(app.engineName)") { app.extract() }
            Button("Cancelar", role: .cancel) {}
        } message: {
            Text(app.confirmMessage)
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

    // MARK: Agentes

    var agentsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header("Agentes", "Cómo consultan la base tus agentes. Solo pueden leer; lo único que escriben es el registro de qué han usado.")
                    .padding(.bottom, -6)
                setting("Conectar un agente", "Pega este bloque en la configuración de servidores MCP de tu agente (Claude Desktop, Claude Code u otro cliente MCP). El agente tendrá estas herramientas: \(app.agentTools.joined(separator: ", ")).") {
                    Text(app.agentConfig.isEmpty ? "…" : app.agentConfig).font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled).padding(9).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 7))
                    Button(action: app.copyAgentConfig) { Label("Copiar configuración", systemImage: "doc.on.doc") }
                        .disabled(app.agentConfig.isEmpty)
                }
                setting("Probar una pregunta", "Muestra exactamente lo que recibiría un agente: las afirmaciones más relevantes, con su por qué, sus límites y las que las contradicen. No llama a ningún modelo ni gasta nada.") {
                    HStack {
                        TextField("¿Puede AST SpaceMobile dar banda ancha a móviles normales?", text: $app.agentQuestion)
                            .textFieldStyle(.roundedBorder).onSubmit { app.agents("ask") }
                        Button("Probar") { app.agents("ask") }
                            .disabled(app.agentQuestion.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    if !app.agentAnswerNote.isEmpty { Text(app.agentAnswerNote).font(.caption).foregroundStyle(accent) }
                    if !app.agentAnswer.isEmpty {
                        ScrollView {
                            Text(app.agentAnswer).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(9)
                        }.frame(height: 230).background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 7))
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
                setting("Registro de uso", app.agentUses.isEmpty ? "Aquí aparecerá cada decisión que un agente apoye en la base, con las afirmaciones que citó."
                                                                 : "Lo que los agentes han anotado, lo más reciente primero.") {
                    ForEach(app.agentUses.prefix(40)) { use in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(use.purpose).font(.callout).fixedSize(horizontal: false, vertical: true)
                            Text("\(use.at) · \(use.agent) · \(use.claims) afirmaciones").font(.caption).foregroundStyle(.secondary)
                        }.padding(.vertical, 3)
                    }
                }
            }
        }
    }

    // MARK: Contraste

    func pairSide(_ text: String, _ origin: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            Text(origin).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    var contrastPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            header("Contraste", "Qué dicen varias fuentes sobre lo mismo. Dos vídeos del mismo canal no cuentan como fuentes independientes.")
            HStack(spacing: 10) {
                tile(app.crossStats["canales"] ?? 0, "Canales")
                tile(app.crossStats["entidades"] ?? 0, "Entidades compartidas")
                tile(app.crossStats["apoyos"] ?? 0, "Coincidencias entre canales", accent)
                tile(app.crossStats["contradicciones"] ?? 0, "Contradicciones", .orange)
            }.padding(.bottom, 14)
            HStack(spacing: 12) {
                Image(systemName: "arrow.left.arrow.right").font(.title3).foregroundStyle(accent)
                VStack(alignment: .leading, spacing: 3) {
                    Text(app.contrastHeadline).font(.system(size: 13, weight: .semibold))
                    Text("El modelo compara, entidad por entidad, las afirmaciones de canales distintos y señala cuáles coinciden, cuáles se contradicen y cuáles se matizan.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if app.busy == .contrast {
                    ProgressView().controlSize(.small)
                    Button("Pausar", action: app.cancel)
                } else {
                    Button { app.cross("run") } label: { Label("Contrastar vídeos", systemImage: "arrow.right").padding(.horizontal, 4) }
                        .buttonStyle(.borderedProminent).tint(accent).disabled(app.busy != nil || (app.crossStats["pendientes"] ?? 0) == 0)
                }
            }
            .padding(13).background(accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
            .padding(.bottom, 14)

            Picker("", selection: $app.crossFilter) {
                Text("Contradicciones (\(app.crossStats["contradicciones"] ?? 0))").tag("contradicts")
                Text("Coincidencias (\(app.crossStats["apoyos"] ?? 0))").tag("supports")
                Text("Matices (\(app.crossStats["matices"] ?? 0))").tag("refines")
            }.pickerStyle(.segmented).labelsHidden().frame(width: 460).padding(.bottom, 8)

            if app.shownPairs.isEmpty {
                emptyState("arrow.left.arrow.right", "Nada que mostrar aquí todavía",
                           (app.crossStats["pendientes"] ?? 0) > 0 ? "Pulsa «Contrastar vídeos» para cruzar lo que dicen los distintos canales."
                                                                  : "No hay parejas de este tipo entre canales distintos.")
            } else {
                rows(app.shownPairs) { pair in
                    VStack(alignment: .leading, spacing: 6) {
                        if !pair.entity.isEmpty { Text(pair.entity.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(accent) }
                        pairSide(pair.aText, pair.aFrom)
                        Image(systemName: pair.relation == "contradicts" ? "arrow.up.arrow.down" : "equal")
                            .font(.caption).foregroundStyle(pair.relation == "contradicts" ? Color.orange : accent)
                        pairSide(pair.bText, pair.bFrom)
                        if !pair.reason.isEmpty { Text(pair.reason).font(.caption).italic().foregroundStyle(.secondary) }
                    }
                }
            }
        }
    }

    // MARK: Limpieza

    var cleanupPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            header("Limpieza", "Entidades con varios nombres y afirmaciones repetidas. Nada se fusiona sin que lo apruebes.")
            HStack(spacing: 10) {
                tile(app.catalogStats["entities"] ?? 0, "Entidades")
                tile(app.proposals.count, "Fusiones propuestas", accent)
                tile(app.catalogStats["merged"] ?? 0, "Ya fusionadas", accent)
                tile(app.catalogStats["duplicates"] ?? 0, "Afirmaciones repetidas", .orange)
            }.padding(.bottom, 14)
            HStack(spacing: 12) {
                Image(systemName: "wand.and.stars").font(.title3).foregroundStyle(accent)
                VStack(alignment: .leading, spacing: 3) {
                    Text("La misma cosa suele acabar con varios nombres: en dos idiomas, con siglas o en plural.")
                        .font(.system(size: 13, weight: .semibold))
                    Toggle("Usar el modelo (\(app.engineName)) para equivalencias que no se ven en el nombre: traducciones y siglas. Son unas cuatro llamadas.",
                           isOn: $app.useModelForCleanup).toggleStyle(.checkbox).font(.caption).disabled(app.busy != nil)
                }
                Spacer()
                if app.busy == .cleanup { ProgressView().controlSize(.small) }
                if (app.catalogStats["duplicates"] ?? 0) > 0 {
                    Button("Quitar repetidas") { app.catalog("dedupe") }.disabled(app.busy != nil)
                        .help("Retira las afirmaciones del mismo vídeo con la misma cita, conservando la más completa.")
                }
                Button { app.catalog("propose") } label: { Label("Buscar duplicados", systemImage: "magnifyingglass").padding(.horizontal, 4) }
                    .buttonStyle(.borderedProminent).tint(accent).disabled(app.busy != nil)
            }
            .padding(13).background(accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
            .padding(.bottom, 14)

            if !app.doneMerges.isEmpty {
                DisclosureGroup("Fusiones hechas (\(app.doneMerges.count))", isExpanded: $app.showDoneMerges) {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(app.doneMerges) { merge in
                                HStack(spacing: 6) {
                                    Text(merge.drop).foregroundStyle(.secondary)
                                    Image(systemName: "arrow.right").font(.caption2).foregroundStyle(.secondary)
                                    Text(merge.keep)
                                    Text("· \(merge.claims) afirmaciones").font(.caption).foregroundStyle(.secondary)
                                    Spacer()
                                    Button("Deshacer") { app.undoMerge(merge) }.controlSize(.small).disabled(!merge.undoable || app.busy != nil)
                                        .help(merge.undoable ? "Recrea la entidad absorbida y le devuelve sus afirmaciones." : "Fusión anterior a esta versión: no se guardó qué se movió.")
                                }.font(.callout).lineLimit(1).padding(.vertical, 4)
                            }
                        }
                    }.frame(maxHeight: 190)
                }.font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.bottom, 12)
            }
            if app.proposals.isEmpty {
                emptyState("checkmark.seal", "No hay fusiones pendientes", "Pulsa «Buscar duplicados» para revisar el catálogo.\nVerás cada propuesta antes de aplicarla.")
            } else {
                HStack {
                    Text("FUSIONES PROPUESTAS · \(app.chosenProposals.count) de \(app.proposals.count) marcadas")
                        .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Spacer()
                    Button("Todas") { app.unchecked = [] }
                    Button("Ninguna") { app.unchecked = Set(app.proposals.map { $0.id }) }
                    Button("Descartar marcadas") { app.catalog("dismiss") }.disabled(app.chosenProposals.isEmpty)
                        .help("No volverán a proponerse.")
                    Button { app.confirmMerge = true } label: { Label("Fusionar marcadas", systemImage: "arrow.triangle.merge") }
                        .buttonStyle(.borderedProminent).tint(accent).disabled(app.chosenProposals.isEmpty)
                }.controlSize(.small).disabled(app.busy != nil).padding(.bottom, 6)
                rows(app.proposals) { proposal in
                    HStack(alignment: .top, spacing: 10) {
                        Button {
                            if app.unchecked.contains(proposal.id) { app.unchecked.remove(proposal.id) } else { app.unchecked.insert(proposal.id) }
                        } label: {
                            Image(systemName: app.unchecked.contains(proposal.id) ? "square" : "checkmark.square.fill")
                                .foregroundStyle(app.unchecked.contains(proposal.id) ? Color.secondary : accent)
                        }.buttonStyle(.plain).disabled(app.busy != nil).padding(.top, 1)
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(proposal.dropName).strikethrough(!app.unchecked.contains(proposal.id), color: .secondary)
                                Image(systemName: "arrow.right").font(.caption).foregroundStyle(.secondary)
                                Text(proposal.keepName).fontWeight(.semibold)
                            }.lineLimit(1)
                            Text(app.proposalDetail(proposal)).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                            if !proposal.sameType {
                                Label("Son de tipos distintos: revísala con cuidado.", systemImage: "exclamationmark.triangle")
                                    .font(.caption).foregroundStyle(Color.orange)
                            }
                        }
                        Spacer()
                        Text(proposal.origin == "model" ? "modelo" : "nombre").font(.caption).foregroundStyle(.secondary)
                            .padding(.horizontal, 8).padding(.vertical, 3).background(panel, in: Capsule())
                    }
                }
            }
        }
        .confirmationDialog("¿Fusionar \(app.chosenProposals.count) entidades?", isPresented: $app.confirmMerge) {
            Button("Fusionar") { app.catalog("apply") }
            Button("Cancelar", role: .cancel) {}
        } message: {
            Text("Las afirmaciones de cada entidad tachada pasarán a la que queda, y su nombre se conservará como alias. Antes se guarda una copia de la base junto al fichero.")
        }
    }

    // MARK: Ajustes

    func setting<Content: View>(_ title: String, _ note: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 13, weight: .semibold))
            content()
            Text(note).font(.caption).foregroundStyle(.secondary)
        }
        .padding(14).frame(maxWidth: 720, alignment: .leading)
        .background(panel, in: RoundedRectangle(cornerRadius: 12))
    }

    var settingsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header("Ajustes", "Se aplican a la cola, a la extracción y al informe rápido.").padding(.bottom, -6)
                setting("Clave API de Meta", "Para el informe y la extracción con Muse Spark. Se guarda en el Llavero de macOS, nunca en un fichero.") {
                    HStack {
                        SecureField("Pega tu clave", text: $app.key).textFieldStyle(.roundedBorder).onSubmit(app.saveKey)
                        Button("Guardar", action: app.saveKey).disabled(app.key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        Button("Olvidar", action: app.forgetKey).disabled(app.key.isEmpty)
                    }
                }
                setting("Motor de extracción", "El modelo que lee las transcripciones y extrae las afirmaciones. Muse Spark usa tu clave de Meta y se factura en tu cuenta; Claude Code usa el CLI `claude` instalado en tu Mac y tu suscripción.") {
                    Picker("", selection: $app.engine) {
                        Text("Muse Spark (clave de Meta)").tag("meta")
                        Text("Claude Code").tag("claude-code")
                    }.labelsHidden().fixedSize().disabled(app.busy == .library)
                }
                setting("Velocidad de extracción", "El modelo gasta casi todo el tiempo en razonar antes de escribir: con razonamiento bajo responde mucho antes, a cambio de pensar menos cada afirmación. Si tu modelo no admite el ajuste, se usa el suyo sin más. Enviar varios tramos a la vez divide el tiempo; si Meta limita las peticiones, la app espera y sigue, pero baja el número si lo ves a menudo.") {
                    HStack(spacing: 18) {
                        HStack(spacing: 6) {
                            Text("Razonamiento").font(.caption)
                            Picker("", selection: $app.reasoning) {
                                Text("Bajo (más rápido)").tag("low")
                                Text("Medio").tag("medium")
                                Text("El del modelo").tag("")
                            }.labelsHidden().fixedSize()
                        }
                        HStack(spacing: 6) {
                            Text("Tramos a la vez").font(.caption)
                            Picker("", selection: $app.workers) {
                                ForEach(1...4, id: \.self) { Text("\($0)").tag($0) }
                            }.labelsHidden().fixedSize()
                        }
                    }.disabled(app.busy == .library)
                }
                setting("Presupuesto de extracción", "En dólares. La extracción se detiene sola al llegar al tope de cada tanda; como mucho lo supera en las llamadas que estén en curso en ese momento. Los precios son los de tu modelo por millón de tokens (consulta la tarifa de tu proveedor); con Claude Code se usa el coste que informa el propio CLI.") {
                    HStack(spacing: 14) {
                        HStack(spacing: 5) { Text("Tope por tanda").font(.caption); TextField("5", text: $app.budget).textFieldStyle(.roundedBorder).frame(width: 64); Text("$").font(.caption) }
                        HStack(spacing: 5) { Text("Entrada").font(.caption); TextField("—", text: $app.priceIn).textFieldStyle(.roundedBorder).frame(width: 64); Text("$/M").font(.caption) }
                        HStack(spacing: 5) { Text("Salida").font(.caption); TextField("—", text: $app.priceOut).textFieldStyle(.roundedBorder).frame(width: 64); Text("$/M").font(.caption) }
                    }.disabled(app.busy == .library)
                }
                setting("Modelo", "Identificador del modelo de Meta, para el informe y para la extracción con Muse Spark.") {
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
