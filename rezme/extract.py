"""Extracción estructurada: transcripción guardada -> afirmaciones verificadas.

Por cada tramo se llama a un modelo que devuelve JSON con afirmaciones,
entidades e implicaciones. Garantías:
  - La transcripción es un dato no confiable: de la respuesta solo se leen los
    campos del esquema (lista blanca); cualquier otro se ignora y se anota.
  - Nada se inserta sin pasar por `Claim.validate()` / `Implication.validate()`.
  - Si la respuesta no valida se reintenta una vez con el error; lo que siga sin
    validar se descarta y el motivo queda en las estadísticas del run.
  - Cada afirmación se verifica contra el tramo (`verify`) antes de guardarse.
  - Reextraer con la misma transcripción, prompt, backend y modelo reutiliza el
    run y no repite los tramos ya hechos.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from . import prompts
from .backends import Backend, BackendUnavailable
from .chunking import Chunk, chunk_transcript, hms, render
from .schema import (
    DECISION_TAGS, ENTITY_TYPES, IMPLICATION_BASES, KNOWLEDGE_FIELDS, MAX_ITEMS, RELATION_TYPES,
    Claim, Entity, Implication, ValidationError, normalize_name, parse_ts,
)
from .store import AmbiguousEntity, Store
from .verify import MIN_QUOTE_TOKENS, find_quote, verify_claim

_TEXT_FIELDS = ("domain", "metric_name", "metric_unit", "metric_period", "currency", "as_of",
                "valid_from", "valid_to", "horizon", "quote")
_CLAIM_KEYS = frozenset(_TEXT_FIELDS) | {
    "statement", "type", "entity", "evidence_grade", "stance", "metric_value", "ts_start",
    "ts_end", "confidence", "attrs", "implications", "title", "tags", "relations",
    *KNOWLEDGE_FIELDS}
_IMPLICATION_KEYS = frozenset({"target", "direction", "basis", "mechanism", "horizon",
                               "strength", "confidence", "quote", "conditional_on"})
_ENTITY_KEYS = frozenset({"name", "type", "aliases", "external_ids"})
_TOP_KEYS = frozenset({"entities", "claims"})
# Claves de attrs que escribe el sistema; el modelo no puede fijarlas.
_RESERVED_ATTRS = frozenset({"grounding", "tramo", "entidad"})
_MAX_ATTRS = 12
_MAX_ATTR_TEXT = 300
# Dos tramos seguidos con fallo del backend: se aborta (se puede reanudar después).
_MAX_CONSECUTIVE_FAILURES = 2


class ExtractionError(RuntimeError):
    """La extracción no pudo completarse (fallo del backend)."""


@dataclass
class ImplicationCandidate:
    implication: Implication
    quote: str | None


@dataclass
class Candidate:
    claim: Claim
    entity: str | None
    implications: list[ImplicationCandidate] = field(default_factory=list)
    index: int = -1  # posición en la respuesta, para las relaciones entre afirmaciones
    relations: list[tuple[int, str]] = field(default_factory=list)  # (posición destino, relación)


@dataclass
class Parsed:
    candidates: list[Candidate] = field(default_factory=list)
    entities: dict[str, Entity] = field(default_factory=dict)  # nombre o alias normalizado
    errors: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    fatal: bool = False  # la respuesta entera es inservible
    truncated: bool = False  # respuesta cortada: solo se han rescatado las afirmaciones completas


@dataclass
class ExtractResult:
    run_id: int
    reused_run: bool
    prompt_version: str
    chunks: int = 0
    chunks_processed: int = 0
    chunks_skipped: int = 0
    chunks_failed: int = 0
    llm_calls: int = 0
    claims_new: int = 0
    verified: int = 0
    ungrounded: int = 0
    implications: int = 0
    discarded: int = 0
    superseded: int = 0


# --------------------------------------------------------------------------
# Respuesta del modelo -> candidatos validados
# --------------------------------------------------------------------------

def parse_json(text: str) -> Any:
    """JSON de la respuesta, tolerando bloques de código o texto alrededor."""
    body = text.strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", body, flags=re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            raise ValidationError("la respuesta no contiene un objeto JSON") from None
        try:
            return json.loads(body[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValidationError(f"la respuesta no es JSON válido ({exc.msg}, línea "
                                  f"{exc.lineno})") from None


def _complete_objects(text: str, start: int) -> list[Any]:
    """Objetos JSON completos de una lista que empieza en `text[start] == '['`, aunque esté cortada."""
    items: list[Any] = []
    depth, begin, in_string, escaped = 0, -1, False, False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    items.append(json.loads(text[begin:i + 1]))
                except json.JSONDecodeError:
                    pass
        elif ch == "]" and depth == 0:
            break
    return items


def salvage_truncated(text: str) -> dict[str, Any] | None:
    """Si la respuesta se cortó a mitad (límite de longitud), rescata las afirmaciones completas."""
    body = text.strip().rstrip("`").rstrip()
    if body.endswith("}"):
        return None  # no está cortada: que la valide el camino normal
    lists: dict[str, list[Any]] = {}
    for key in ("entities", "claims"):
        match = re.search(rf'"{key}"\s*:\s*\[', body)
        lists[key] = _complete_objects(body, match.end() - 1) if match else []
    return lists if lists["claims"] else None


def _text(item: dict[str, Any], key: str, *, numbers: bool = False) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if numbers and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise ValidationError(f"{key} debe ser texto o null")
    return value.strip() or None


def _number(item: dict[str, Any], key: str) -> float | None:
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{key} debe ser un número o null")
    return float(value)


def _ts(value: Any) -> float | None:
    try:
        return parse_ts(value) if value is not None else None
    except ValidationError:
        return None  # el anclaje real lo fija la verificación


def _scalar(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_ATTR_TEXT
    return isinstance(value, (int, float)) and math.isfinite(value)


def _clean_attrs(raw: Any, path: str, ignored: list[str]) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("attrs debe ser un objeto")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        ok = (isinstance(key, str) and 0 < len(key) <= 40 and key not in _RESERVED_ATTRS
              and len(out) < _MAX_ATTRS
              and (_scalar(value) or (isinstance(value, list) and len(value) <= 10
                                      and all(_scalar(v) for v in value))))
        if ok:
            out[key] = value
        else:
            ignored.append(f"{path}.attrs.{key}")
    return out


def _unknown(item: dict[str, Any], allowed: frozenset[str], path: str, ignored: list[str]) -> None:
    ignored.extend(f"{path}.{k}" for k in item if k not in allowed)


def _build_entity(item: Any, path: str, ignored: list[str]) -> Entity:
    if not isinstance(item, dict):
        raise ValidationError("debe ser un objeto")
    _unknown(item, _ENTITY_KEYS, path, ignored)
    name, type_ = _text(item, "name"), _text(item, "type")
    if not name:
        raise ValidationError("name vacío")
    aliases = item.get("aliases") or []
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        raise ValidationError("aliases debe ser una lista de textos")
    ids = item.get("external_ids") or {}
    if not isinstance(ids, dict) or not all(
            isinstance(k, str) and isinstance(v, (str, int)) and not isinstance(v, bool)
            for k, v in ids.items()):
        raise ValidationError("external_ids debe ser un objeto de textos")
    return Entity(type_ or "", name, [a for a in aliases if a.strip()],
                  {k: str(v) for k, v in ids.items()}).validate()


def _build_implication(item: Any, path: str, ignored: list[str]) -> ImplicationCandidate:
    if not isinstance(item, dict):
        raise ValidationError("debe ser un objeto")
    _unknown(item, _IMPLICATION_KEYS, path, ignored)
    target = _text(item, "target")
    if not target:
        raise ValidationError("target vacío")
    implication = Implication(
        claim_id=0, direction=item.get("direction"), basis=item.get("basis"),
        target_label=target, mechanism=_text(item, "mechanism"), horizon=_text(item, "horizon"),
        strength=_number(item, "strength"), confidence=_number(item, "confidence"),
        conditional_on=_text(item, "conditional_on")).validate()
    return ImplicationCandidate(implication, _text(item, "quote"))


def _knowledge_items(raw: Any, name: str, path: str, ignored: list[str]) -> list[dict[str, Any]]:
    """Lista de {"text", "basis"[, "quote"]}. Un texto suelto se toma como deducido por el sistema."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError(f"{name} debe ser una lista")
    items: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if len(items) >= MAX_ITEMS:
            ignored.append(f"{path}.{name}[{i}]")
            continue
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str) or not entry["text"].strip():
            raise ValidationError(f"{name}[{i}] debe ser un objeto con text")
        ignored.extend(f"{path}.{name}[{i}].{k}" for k in entry if k not in ("text", "basis", "quote"))
        basis = entry.get("basis") or "inferred_by_system"
        if basis not in IMPLICATION_BASES:
            raise ValidationError(f"{name}[{i}].basis no válido: {basis!r}")
        item: dict[str, Any] = {"text": entry["text"].strip(), "basis": basis}
        quote = entry.get("quote")
        if isinstance(quote, str) and quote.strip():
            item["quote"] = quote.strip()
        items.append(item)
    return items


def _build_claim(item: Any, source_id: int, path: str, parsed: Parsed) -> Candidate:
    if not isinstance(item, dict):
        raise ValidationError("debe ser un objeto")
    _unknown(item, _CLAIM_KEYS, path, parsed.ignored)
    statement = item.get("statement")
    if not isinstance(statement, str):
        raise ValidationError("statement debe ser texto")
    fields = {k: _text(item, k, numbers=k in ("as_of", "valid_from", "valid_to", "metric_period"))
              for k in _TEXT_FIELDS}
    claim = Claim(
        source_id=source_id, statement=statement.strip(), type=item.get("type"),
        evidence_grade=item.get("evidence_grade") or "none", stance=item.get("stance") or "n/a",
        metric_value=_number(item, "metric_value"), confidence=_number(item, "confidence"),
        ts_start=_ts(item.get("ts_start")), ts_end=_ts(item.get("ts_end")),
        attrs=_clean_attrs(item.get("attrs"), path, parsed.ignored),
        title=_text(item, "title"), **fields,
        **{name: _knowledge_items(item.get(name), name, path, parsed.ignored)
           for name in KNOWLEDGE_FIELDS})
    raw_tags = item.get("tags") or []
    if not isinstance(raw_tags, list):
        raise ValidationError("tags debe ser una lista")
    for tag in raw_tags:  # las etiquetas desconocidas se ignoran, no invalidan la afirmación
        if tag in DECISION_TAGS and tag not in claim.tags:
            claim.tags.append(tag)
        elif tag not in DECISION_TAGS:
            parsed.ignored.append(f"{path}.tags.{tag}")
    if claim.ts_start is not None and claim.ts_end is not None and claim.ts_end < claim.ts_start:
        claim.ts_end = None
    claim.validate()
    candidate = Candidate(claim, _text(item, "entity"))
    for raw in item.get("relations") or [] if isinstance(item.get("relations"), list) else []:
        if (isinstance(raw, dict) and isinstance(raw.get("to"), int) and not isinstance(raw["to"], bool)
                and raw.get("relation") in RELATION_TYPES):
            candidate.relations.append((raw["to"], raw["relation"]))
        else:
            parsed.ignored.append(f"{path}.relations")
    raw_implications = item.get("implications") or []
    if not isinstance(raw_implications, list):
        raise ValidationError("implications debe ser una lista")
    for j, raw in enumerate(raw_implications):
        sub = f"{path}.implications[{j}]"
        try:
            candidate.implications.append(_build_implication(raw, sub, parsed.ignored))
        except ValidationError as exc:
            parsed.errors.append(f"{sub}: {exc}")
    return candidate


def parse_response(text: str, source_id: int) -> Parsed:
    """Valida la respuesta. Los elementos no válidos se quedan fuera y en `errors`."""
    parsed = Parsed()
    try:
        try:
            data = parse_json(text)
        except ValidationError:
            data = salvage_truncated(text)
            if data is None:
                raise
            parsed.truncated = True
        if not isinstance(data, dict):
            raise ValidationError("la respuesta debe ser un objeto JSON")
        if not isinstance(data.get("claims"), list):
            raise ValidationError("falta la lista `claims`")
        if not isinstance(data.get("entities") or [], list):
            raise ValidationError("`entities` debe ser una lista")
    except ValidationError as exc:
        parsed.errors.append(str(exc))
        parsed.fatal = True
        return parsed

    _unknown(data, _TOP_KEYS, "respuesta", parsed.ignored)
    for i, raw in enumerate(data.get("entities") or []):
        path = f"entities[{i}]"
        try:
            entity = _build_entity(raw, path, parsed.ignored)
        except ValidationError as exc:
            parsed.errors.append(f"{path}: {exc}")
            continue
        for name in (entity.canonical_name, *entity.aliases):
            parsed.entities.setdefault(normalize_name(name), entity)
    for i, raw in enumerate(data["claims"]):
        path = f"claims[{i}]"
        try:
            candidate = _build_claim(raw, source_id, path, parsed)
            candidate.index = i
            parsed.candidates.append(candidate)
        except ValidationError as exc:
            parsed.errors.append(f"{path}: {exc}")
    return parsed


# --------------------------------------------------------------------------
# Prompt y llamada por tramo
# --------------------------------------------------------------------------

def build_user_prompt(source: dict[str, Any], chunk: Chunk, total: int,
                      version: str = prompts.PROMPT_VERSION) -> str:
    # Los ángulos se neutralizan para que el texto no pueda cerrar la etiqueta.
    transcript = render(chunk).replace("<", "‹").replace(">", "›")
    return prompts.fill(
        prompts.load("user", version),
        TITULO=source.get("title") or "—", CANAL=source.get("channel") or "—",
        FECHA=source.get("published_at") or "desconocida", INDICE=chunk.index + 1, TOTAL=total,
        CAPITULO=f" (capítulo «{chunk.title}»)" if chunk.title else "",
        INICIO=hms(chunk.start), FIN=hms(chunk.end), NONCE=secrets.token_hex(4),
        TRANSCRIPCION=transcript)


def extract_chunk(call: Callable[[str, str], str], system: str, user: str, source_id: int,
                  version: str = prompts.PROMPT_VERSION) -> tuple[Parsed, int]:
    """Llama al modelo y valida; reintenta una vez con los errores. Devuelve (resultado, llamadas)."""
    first_text = call(system, user)
    first = parse_response(first_text, source_id)
    if not first.errors:
        return first, 1
    retry = prompts.fill(prompts.load("retry", version), PROMPT=user, RESPUESTA=first_text,
                         ERRORES="\n".join(f"- {e}" for e in first.errors))
    second = parse_response(call(system, retry), source_id)
    # Si el reintento es inservible, se conserva lo válido de la primera respuesta.
    final = first if second.fatal and not first.fatal else second
    return final, 2


# --------------------------------------------------------------------------
# Entidades
# --------------------------------------------------------------------------

def resolve_entity(store: Store, name: str, known: dict[str, Entity], *,
                   create: bool) -> tuple[int | None, dict[str, Any] | None]:
    """Id de la entidad, o None con una nota. Un nombre ambiguo nunca se adivina."""
    try:
        entity_id = store.resolve_entity(name)
    except AmbiguousEntity:
        return None, {"nombre": name, "motivo": "ambigua",
                      "candidatos": store.entity_candidates(name)}
    if entity_id is not None:
        return entity_id, None
    spec = known.get(normalize_name(name))
    if spec is None:
        return None, {"nombre": name, "motivo": "desconocida y sin tipo en la respuesta"}
    if not create:
        return None, {"nombre": name, "motivo": "no se crea desde una afirmación sin verificar"}
    # No se añaden alias que ya apunten a otra entidad: la volverían ambigua.
    aliases = [a for a in spec.aliases if not store.entity_candidates(a)]
    return store.upsert_entity(spec.type, spec.canonical_name, aliases, spec.external_ids), None


# --------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------

def _store_chunk(store: Store, parsed: Parsed, chunk: Chunk, run_id: int, transcript_id: int,
                 result: ExtractResult) -> dict[str, Any]:
    info = {"claims": 0, "verified": 0, "ungrounded": 0, "implicaciones": 0,
            "implicaciones_degradadas": 0, "conocimiento_degradado": 0, "relaciones": 0}

    def said_by_author(quote: str | None) -> bool:
        return (quote is not None and len(normalize_name(quote).split()) >= MIN_QUOTE_TOKENS
                and find_quote(quote, chunk)[1] is not None)

    stored: dict[int, int] = {}  # posición en la respuesta -> id de la afirmación
    for candidate in parsed.candidates:
        claim = candidate.claim
        claim.run_id, claim.transcript_id = run_id, transcript_id
        claim.attrs["tramo"] = {"indice": chunk.index, "inicio": chunk.start, "fin": chunk.end}
        for name in KNOWLEDGE_FIELDS:  # sin prueba literal, no se atribuye al autor
            for item in getattr(claim, name):
                if item["basis"] == "stated_by_source" and not said_by_author(item.get("quote")):
                    item["basis"] = "inferred_by_system"
                    item.pop("quote", None)
                    info["conocimiento_degradado"] += 1
                elif item["basis"] != "stated_by_source":
                    item.pop("quote", None)
        grounding = verify_claim(claim, chunk)
        if candidate.entity:
            claim.entity_id, note = resolve_entity(store, candidate.entity, parsed.entities,
                                                   create=grounding.ok)
            if note:
                claim.attrs["entidad"] = note
        claim_id, created = store.add_claim(claim)
        stored[candidate.index] = claim_id
        if not created:
            continue
        info["claims"] += 1
        info[claim.status] += 1
        for item in candidate.implications:
            implication = item.implication
            implication.claim_id = claim_id
            if implication.basis == "stated_by_source" and not said_by_author(item.quote):
                implication.basis = "inferred_by_system"  # sin prueba literal, no se atribuye al autor
                info["implicaciones_degradadas"] += 1
            target_id, _ = resolve_entity(store, implication.target_label or "", parsed.entities,
                                          create=grounding.ok)
            implication.target_entity_id = target_id
            store.add_implication(implication)
            info["implicaciones"] += 1
    for candidate in parsed.candidates:
        for target, relation in candidate.relations:
            source_id, target_id = stored.get(candidate.index), stored.get(target)
            if source_id and target_id and source_id != target_id:
                info["relaciones"] += store.add_relation(source_id, relation, target_id)
    result.claims_new += info["claims"]
    result.verified += info["verified"]
    result.ungrounded += info["ungrounded"]
    result.implications += info["implicaciones"]
    result.discarded += len(parsed.errors)
    return info


def extract_source(store: Store, source_id: int, backend: Backend, *, domain: str | None = None,
                   force: bool = False, prompt_version: str = prompts.PROMPT_VERSION,
                   progress: Callable[[str], None] | None = None,
                   workers: int = 1) -> ExtractResult:
    """Extrae y verifica las afirmaciones de una fuente ya ingerida.

    Con `workers` > 1 las llamadas al modelo de varios tramos se hacen a la vez; la
    validación y la escritura en la base siguen siendo una a una y en orden.
    """
    source = store.get_source_by_id(source_id)
    if source is None:
        raise ValueError(f"La fuente {source_id} no existe.")
    transcript = store.latest_transcript(source_id)
    if transcript is None:
        raise ValueError(f"La fuente {source_id} no tiene transcripción guardada.")
    say = progress or (lambda _msg: None)

    system = prompts.system_prompt(domain, prompt_version)
    label = f"{prompt_version}+{domain}" if domain else prompt_version
    chapters = json.loads(source["chapters_json"]) if source.get("chapters_json") else None
    chunks = chunk_transcript(transcript["cues"], chapters, source.get("duration_s"))

    run = None if force else store.find_run(source_id, transcript["id"], label,
                                            backend.name, backend.model)
    if run is None:
        run_id = store.start_run(model=backend.model, backend=backend.name, prompt_version=label,
                                 source_id=source_id, transcript_id=transcript["id"])
        stats: dict[str, Any] = {"dominio": domain, "tramos": {}}
    else:
        run_id, stats = run["id"], run["stats"]
        stats.setdefault("tramos", {})
    result = ExtractResult(run_id, run is not None, label, chunks=len(chunks))
    # El backend acumula consumo entre vídeos; a cada run se le apunta solo lo suyo.
    base_cost, base_in, base_out = backend.cost_usd or 0.0, backend.input_tokens, backend.output_tokens
    previous_cost = (run or {}).get("cost_usd") or 0.0
    usage = stats.setdefault("consumo", {"entrada": 0, "salida": 0, "llamadas": 0,
                                         "caracteres_prompt": 0, "caracteres_transcripcion": 0})
    seen_in, seen_out, seen_reasoning = base_in, base_out, backend.reasoning_tokens

    todo: list[Chunk] = []
    for chunk in chunks:
        done = stats["tramos"].get(str(chunk.index))
        if done and done.get("estado") == "ok" and (done.get("inicio"), done.get("fin")) == (
                chunk.start, chunk.end):
            result.chunks_skipped += 1
        else:
            todo.append(chunk)

    def label(chunk: Chunk) -> str:
        return f"Tramo {chunk.index + 1}/{len(chunks)} [{hms(chunk.start)}–{hms(chunk.end)}]"

    # Si el modelo deja de estar disponible (clave, saldo, tope de gasto), los tramos en cola no
    # deben llegar a llamarlo: cancelar los futuros no basta, porque un fallo inmediato deja libre
    # el hilo antes de que el hilo principal se entere.
    halted = threading.Event()

    def work(chunk: Chunk) -> tuple[Parsed, int, int]:
        if halted.is_set():
            raise BackendUnavailable("extracción detenida")
        user = build_user_prompt(source, chunk, len(chunks), prompt_version)
        try:
            parsed, calls = extract_chunk(backend.call, system, user, source_id, prompt_version)
        except BackendUnavailable:
            halted.set()
            raise
        return parsed, calls, len(user)

    def outcomes() -> Iterator[tuple[Chunk, tuple[Parsed, int, int] | None, Exception | None]]:
        """Resultado de cada tramo, en orden. Un fallo de acceso al modelo se propaga tal cual."""
        if workers <= 1 or len(todo) <= 1:
            for chunk in todo:
                say(f"  {label(chunk)}…")
                try:
                    yield chunk, work(chunk), None
                except BackendUnavailable:
                    raise
                except Exception as exc:  # fallo del backend: se anota y se puede reanudar
                    yield chunk, None, exc
            return
        pool = ThreadPoolExecutor(max_workers=min(workers, len(todo)))
        try:
            say(f"  {len(todo)} tramos, {min(workers, len(todo))} a la vez…")
            futures = [(chunk, pool.submit(work, chunk)) for chunk in todo]
            for position, (chunk, future) in enumerate(futures, 1):
                try:
                    outcome = future.result()
                except BackendUnavailable:
                    raise
                except Exception as exc:
                    yield chunk, None, exc
                else:
                    say(f"  {label(chunk)} hecho ({position} de {len(todo)})")
                    yield chunk, outcome, None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # no se lanzan los tramos que faltaban

    failures = 0
    with closing(outcomes()) as stream:
        for chunk, outcome, exc in stream:
            entry: dict[str, Any] = {"inicio": chunk.start, "fin": chunk.end}
            if outcome is None:
                entry.update(estado="error", error=str(exc))
                stats["tramos"][str(chunk.index)] = entry
                store.update_run(run_id, stats=stats)
                result.chunks_failed += 1
                failures += 1
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise ExtractionError(
                        f"El backend {backend.name} ha fallado en {failures} tramos seguidos: {exc}. "
                        "Vuelve a lanzar la extracción para reanudarla.") from exc
                continue
            parsed, calls, user_chars = outcome
            failures = 0
            result.llm_calls += calls
            usage["entrada"] += backend.input_tokens - seen_in
            usage["salida"] += backend.output_tokens - seen_out
            usage["razonamiento"] = usage.get("razonamiento", 0) + backend.reasoning_tokens - seen_reasoning
            seen_in, seen_out = backend.input_tokens, backend.output_tokens
            seen_reasoning = backend.reasoning_tokens
            usage["llamadas"] += calls
            usage["caracteres_prompt"] += len(system) + user_chars
            usage["caracteres_transcripcion"] += len(render(chunk))
            if parsed.truncated:
                entry["truncada"] = True
            entry.update(estado="ok", reintento=calls > 1, descartes=parsed.errors,
                         campos_ignorados=parsed.ignored,
                         **_store_chunk(store, parsed, chunk, run_id, transcript["id"], result))
            stats["tramos"][str(chunk.index)] = entry
            spent = (backend.cost_usd or 0.0) - base_cost
            store.update_run(run_id, stats=stats,
                             cost_usd=previous_cost + spent if backend.cost_usd is not None else None)
            result.chunks_processed += 1

    complete = all(stats["tramos"].get(str(c.index), {}).get("estado") == "ok" for c in chunks)
    if complete and result.chunks_processed:
        # El histórico se conserva; los agentes solo ven la extracción más reciente.
        result.superseded = store.supersede_previous_runs(source_id, run_id)
    return result
