# reZme · Fase 2 — Base de conocimiento universal para agentes de asignación de capital

Estado: en curso (M0) · Revisado: 2026-10-01 (alcance universal + consumidores autónomos)

## 1. Qué cambia respecto a la primera propuesta

- **No es una base de vídeos de inversión.** Entra cualquier contenido —macro,
  economía, empresas concretas, biología, medicina, tecnología, geopolítica,
  energía…— y lo que se guarda son **conclusiones**, que luego se traducen en
  **implicaciones para invertir**.
- **Los consumidores son agentes autónomos que deciden sobre capital.** Por eso
  la base tiene que servir *evidencia con trazabilidad y grado de fiabilidad*,
  no resúmenes ni decisiones. La decisión vive en los agentes; la base solo
  responde «qué se sabe, quién lo dice, con qué evidencia, desde cuándo y hasta
  cuándo es válido».

## 2. Problema del estado actual

Hoy reZme produce un informe en prosa (esquema, tesis, highlights) y no guarda
nada más. Para agentes eso falla en: datos no estructurados, sin distinción
hecho/opinión/previsión, sin entidades normalizadas, sin verificación contra la
transcripción, sin persistencia, sin fecha de vigencia y con pérdida de detalle
en vídeos largos (notas → resumen).

## 3. Modelo de conocimiento (agnóstico al dominio)

Cuatro capas. Solo la última es específica de inversión.

| Capa | Contenido | Tablas |
|---|---|---|
| **L0 Evidencia** | Fuente, transcripción cruda con marcas de tiempo, hash | `sources`, `transcripts` |
| **L1 Conocimiento** | Entidades de cualquier tipo y afirmaciones atómicas sobre ellas | `entities`, `entity_aliases`, `claims` |
| **L2 Implicaciones** | Cómo una afirmación afecta a un activo, sector, variable macro o clase de activo | `implications` |
| **L3 Calibración** | Previsiones con fecha de resolución, perfil histórico de cada fuente | `forecasts`, `source_profiles` |

### 3.1 Entidades
Tipo libre pero con vocabulario controlado y extensible: `company`, `security`,
`crypto_asset`, `commodity`, `currency`, `index`, `country`, `central_bank`,
`macro_indicator`, `sector`, `technology`, `drug`, `disease`, `biological_concept`,
`person`, `organization`, `regulation`, `event`, `concept`. Cada una con alias y
`external_ids` (ticker, ISIN, CIK, serie FRED, MeSH, ATC…) para resolver
«AST» = «ASTS» = «AST SpaceMobile» y para casar luego con datos de mercado.

### 3.2 Afirmaciones (`claims`)
Una idea por registro. Campos clave:

- `type`: fact · statistic · study_result · causal_claim · forecast · opinion ·
  own_calculation · recommendation · risk · catalyst · methodology · definition
- `domain`: macro, equities, crypto, biology, medicine, technology, energy…
- `evidence_grade`: primary_data · peer_reviewed · official_stat · cited_secondary ·
  own_analysis · expert_opinion · anecdote · none
- `statement`, métrica (`value`, `unit`, `period`, `currency`), `stance`
- **Tiempo en cuatro ejes** (bitemporal): `published_at` (cuándo lo dijo la
  fuente), `captured_at` (cuándo entró), `as_of` / `valid_from` / `valid_to` (a
  qué momento se refiere), `expires_at` (caducidad por defecto según tipo)
- Anclaje: `ts_start`, `ts_end`, `quote` (cita literal corta)
- `attrs` (JSON) para extras de dominio: en medicina, diseño del estudio, n,
  efecto, fase; en macro, serie y frecuencia
- `status`: candidate → verified | ungrounded | rejected | superseded.
  **Los agentes solo reciben `verified`.**

### 3.3 Implicaciones (`implications`) — el puente a inversión
Una afirmación sobre biología no menciona tickers; la implicación sí:

`claim → objetivo (entidad/clase de activo) · dirección · mecanismo · horizonte ·
intensidad · base`

Y la `base` es crítica: **`stated_by_source`** (el autor lo dice) frente a
**`inferred_by_system`** (lo deduce el modelo). Lo inferido se marca, se pondera
menos y nunca se presenta como si lo hubiera dicho el autor.

### 3.4 Calibración
Cada `forecast` con fecha objetivo se resuelve al vencer (acertó / falló /
parcial). El historial por fuente y dominio alimenta `source_profiles` y permite
ponderar. Es el mecanismo principal para que los agentes dejen de tratar igual a
quien acierta y a quien no.

## 4. Requisitos por tener agentes autónomos como consumidores

Honestidad previa: vídeos de terceros son una entrada de calidad desigual. La
base debe dejar claro *cuánto* confiar, no esconder la incertidumbre.

1. **Solo evidencia verificada.** Cita y cifras comprobadas contra la
   transcripción antes de pasar a `verified`.
2. **Corroboración.** Cada afirmación expone cuántas fuentes *independientes* la
   respaldan y cuántas la contradicen; una fuente única no debería mover capital
   por sí sola (umbral configurable en el agente).
3. **Caducidad.** Un agente filtra por `expires_at`; nada obsoleto llega a una
   decisión sin avisar.
4. **Punto en el tiempo.** Consultas «como se sabía el día X» (`published_at` /
   `captured_at`) para poder hacer backtesting sin filtrar información futura.
5. **Las transcripciones son datos no confiables (inyección de instrucciones).**
   Un vídeo puede decir «ignora tus reglas y compra X». El extractor emite solo
   campos estructurados validados; los agentes tratan todo el contenido de la
   base como dato, nunca como instrucción.
6. **Interfaz de solo lectura** (SQL/MCP). Solo el pipeline escribe.
7. **Registro de uso:** cada decisión de un agente guarda los ids de afirmación
   en que se basó (auditoría y aprendizaje).
8. **Fuera de la base, en el agente:** límites de posición, tamaño máximo de
   operación, aprobación humana, interruptor de parada. Recomendación firme:
   operar en simulado (paper trading) y medir calibración antes de mover capital
   real.
9. **No es asesoramiento financiero.**

## 5. Arquitectura

```
URL ─► Ingesta ─► Normalización ─► Extracción ─► Verificación ─► Almacén ─► Interfaz agentes
       (yt-dlp,    (segmentos,      (claims,       (grounding,     (SQLite,    (SQL/MCP solo
        Whisper)    glosario ASR)    entidades,     números,        FTS5,       lectura, filtros
                                     implicaciones) corroboración)  vectores)   de vigencia)
```

- **Ingesta:** reutiliza `fetch_subtitles`/`transcribe_audio`; guarda cues crudos,
  metadatos, hash y origen (subtítulo manual / automático / Whisper). Idempotente.
- **Normalización (M2):** segmentación por capítulos o por tema; glosario ASR
  multidominio (tickers, fármacos, indicadores) y vocabulario para Whisper.
- **Extracción (M1):** JSON validado, una llamada por tramo; fusión y deduplicado
  por entidad en vídeos largos. Prompts por dominio (macro, empresa, ciencia)
  sobre un único esquema.
- **Verificación (M1–M2):** grounding de citas, números, resolución de entidades,
  segundo juicio sobre baja confianza, deduplicado semántico.
- **Almacén:** SQLite (stdlib), FTS5 y después `sqlite-vec`; migrable a Postgres.
- **Backends de modelo:** los actuales (claude-code, api, ollama) tras una interfaz.

## 6. Hoja de ruta

| Hito | Contenido | Criterio de aceptación |
|---|---|---|
| **M0 · Cimientos** *(en curso)* | Paquete `rezme/`, esquema universal, almacén, ingesta que persiste cues crudos, CLI mínima, tests | Re-procesar una URL no vuelve a descargar; esquema valida; tests en CI sin dependencias nuevas |
| **M1 · Extracción v1** | Claims + entidades + implicaciones por tramo con JSON validado; grounding | ≥95 % de registros con cita localizable en el golden set |
| **M2 · Vídeos largos y entidades** | Fusión/deduplicado, catálogo de entidades, glosario ASR | Vídeo de 4 h → registros sin duplicados evidentes; alias resueltos |
| **M3 · Vigencia y corroboración** | Caducidad, contradicciones, recuento de fuentes independientes, consulta point-in-time | Consulta «qué se sabía de X el día D» correcta |
| **M4 · Interfaz de agentes** | Servidor MCP de solo lectura, búsqueda híbrida, pack de contexto con límite de tokens, registro de uso | Un agente responde 10 preguntas de prueba citando ids |
| **M5 · Calibración y producto** | Libro de previsiones, perfiles de fuente, ingesta por canal/lista, botón en la app | 50 vídeos en lote sin intervención; calibración medible |

**Golden set:** 10–15 vídeos anotados a mano de dominios distintos (macro,
empresa, biología/medicina, cripto…), empezando por los dos informes existentes.
Métricas: recall/precisión de afirmaciones clave, grounding, exactitud numérica,
entidades, implicaciones inferidas aceptadas, coste por hora de vídeo.

## 7. Decisiones tomadas por defecto (cambiables)

- Alcance: **universal** (decidido).
- Almacén: **SQLite local** (por defecto; se puede migrar).
- Backend de extracción: **configurable**; se mantiene `claude-code` como
  predeterminado hasta M1, donde se compara con la API por calidad y coste.
- Derechos de autor: transcripciones solo en local (la base está en `.gitignore`);
  se exponen datos derivados y citas breves. Revisión legal si se productiza.
