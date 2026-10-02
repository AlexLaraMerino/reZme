"""Limpieza del catálogo: entidades duplicadas y afirmaciones repetidas.

Las entidades se crean una a una mientras se extraen los vídeos, así que la misma
cosa acaba con varios nombres («Federal Reserve» y «Reserva Federal», «Oil» y
«Petróleo»). Aquí se **proponen** fusiones; ninguna se aplica sin que alguien la
acepte. Dos orígenes:
  - `rule`: coincidencias mecánicas (mismo nombre salvo sufijos societarios o
    plural, mismo ticker, nombre que es alias de otra).
  - `model`: equivalencias que requieren entender el significado (traducciones,
    siglas), pedidas a un modelo por lotes de tipos afines.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from .extract import parse_json
from .schema import ValidationError, normalize_name
from .store import Store

# Palabras que no distinguen una entidad de otra.
_NOISE = frozenset("""inc incorporated corp corporation co company ltd limited plc sa ag nv holdings holding
    group grupo technologies technology the el la los las de del""".split())
# Tipos que se revisan juntos: la misma cosa puede haberse clasificado de dos maneras.
TYPE_BATCHES = (
    ("company", "security", "organization"),
    ("technology", "concept", "sector"),
    ("person",),
    ("country", "central_bank", "macro_indicator", "commodity", "index", "crypto_asset", "currency",
     "event", "regulation", "drug", "disease", "biological_concept"),
)
MAX_BATCH = 450
_PROMPT = Path(__file__).resolve().parent / "prompts" / "catalogo.md"


def _key(name: str) -> str:
    """Nombre reducido a lo que identifica: sin sufijos societarios, artículos ni plural simple."""
    tokens = [t for t in normalize_name(name).split() if t not in _NOISE]
    return " ".join(t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t for t in tokens)


def _target(group: list[dict[str, Any]]) -> dict[str, Any]:
    """La entidad que se queda: la más usada; a igualdad, la más antigua."""
    return max(group, key=lambda e: (e["claims"], -e["id"]))


def rule_groups(entities: Iterable[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], str]]:
    """Grupos de entidades del mismo tipo que coinciden mecánicamente, con el motivo."""
    entities = list(entities)
    groups: list[tuple[list[dict[str, Any]], str]] = []
    seen: set[frozenset[int]] = set()

    def add(members: list[dict[str, Any]], reason: str) -> None:
        ids = frozenset(e["id"] for e in members)
        if len(ids) > 1 and ids not in seen:
            seen.add(ids)
            groups.append((members, reason))

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_name: dict[tuple[str, str], dict[str, Any]] = {}
    for entity in entities:
        key = _key(entity["canonical_name"])
        if len(key) >= 3:
            by_key[(entity["type"], key)].append(entity)
        ticker = str(entity["external_ids"].get("ticker") or "").strip().upper()
        if ticker and entity["type"] in ("company", "security"):
            by_ticker[ticker].append(entity)
        by_name[(entity["type"], entity["norm_name"])] = entity
    for members in by_key.values():
        add(members, "mismo nombre salvo sufijos o plural")
    for ticker, members in by_ticker.items():
        add(members, f"mismo ticker ({ticker})")
    for entity in entities:
        for alias in entity["aliases"]:
            other = by_name.get((entity["type"], normalize_name(alias)))
            if other and other["id"] != entity["id"]:
                add([entity, other], f"«{other['canonical_name']}» ya figura como alias")
    return groups


def model_groups(entities: Iterable[dict[str, Any]], call: Callable[[str, str], str],
                 progress: Callable[[str], None] | None = None) -> list[tuple[list[dict[str, Any]], str]]:
    """Grupos de equivalentes propuestos por el modelo. Solo se aceptan ids que se le enviaron."""
    entities = list(entities)
    system = _PROMPT.read_text(encoding="utf-8").strip()
    say = progress or (lambda message: None)
    groups: list[tuple[list[dict[str, Any]], str]] = []
    for types in TYPE_BATCHES:
        pool = sorted((e for e in entities if e["type"] in types), key=lambda e: e["norm_name"])
        for start in range(0, len(pool), MAX_BATCH):
            batch = pool[start:start + MAX_BATCH]
            if len(batch) < 2:
                continue
            say(f"Revisando {len(batch)} entidades ({', '.join(types[:3])}…)")
            lines = [json.dumps({"id": e["id"], "type": e["type"], "name": e["canonical_name"],
                                 **({"aliases": e["aliases"][:4]} if e["aliases"] else {}),
                                 "n": e["claims"]}, ensure_ascii=False) for e in batch]
            answer = call(system, "<entidades>\n" + "\n".join(lines) + "\n</entidades>")
            try:
                data = parse_json(answer)
            except ValidationError:
                continue  # una respuesta inservible no detiene el resto
            by_id = {e["id"]: e for e in batch}
            used: set[int] = set()
            for raw in data.get("groups", []) if isinstance(data, dict) else []:
                ids = raw.get("ids") if isinstance(raw, dict) else None
                if not isinstance(ids, list):
                    continue
                members = [by_id[i] for i in dict.fromkeys(ids)
                           if isinstance(i, int) and not isinstance(i, bool) and i in by_id and i not in used]
                if len(members) < 2:
                    continue
                used.update(e["id"] for e in members)
                reason = raw.get("reason")
                groups.append((members, reason[:200] if isinstance(reason, str) else "equivalentes según el modelo"))
    return groups


def propose_merges(store: Store, call: Callable[[str, str], str] | None = None,
                   progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """Guarda propuestas de fusión nuevas (reglas y, si hay `call`, modelo). Devuelve cuántas por origen."""
    entities = store.list_entities()
    found = {"rule": 0, "model": 0}
    batches = [("rule", rule_groups(entities))]
    if call is not None:
        batches.append(("model", model_groups(entities, call, progress)))
    for origin, groups in batches:
        for members, reason in groups:
            target = _target(members)
            for entity in members:
                if entity["id"] != target["id"]:
                    found[origin] += store.add_merge_proposal(target["id"], entity["id"], origin, reason)
    return found


def apply_merges(store: Store, proposal_ids: Iterable[int]) -> dict[str, int]:
    """Aplica las propuestas elegidas. Devuelve entidades fusionadas y afirmaciones reasignadas."""
    wanted = set(proposal_ids)
    done = {"entidades": 0, "afirmaciones": 0}
    while True:
        # Se relee cada vez: una fusión puede redirigir o eliminar otras propuestas.
        row = next((p for p in store.merge_proposals() if p["id"] in wanted), None)
        if row is None:
            return done
        wanted.discard(row["id"])
        done["afirmaciones"] += store.merge_entities(row["into_entity_id"], row["from_entity_id"],
                                                     origin=row["origin"], reason=row["reason"])
        done["entidades"] += 1


def duplicate_claims(store: Store) -> list[tuple[int, int]]:
    """Pares (se queda, sobra) de afirmaciones verificadas del mismo vídeo con la misma cita.

    Pasa en el solape entre tramos y cuando el modelo repite una idea. Se queda la más completa.
    """
    rows = store.db.execute(
        """SELECT id, source_id, quote, statement, metric_value, mechanism_json, applies_when_json,
                  fails_when_json FROM claims WHERE status='verified' AND quote IS NOT NULL
           ORDER BY source_id, id""").fetchall()
    best: dict[tuple[int, str, float | None], Any] = {}
    pairs: list[tuple[int, int]] = []

    def richness(row: Any) -> tuple[int, int]:
        extra = sum(len(json.loads(row[c])) for c in ("mechanism_json", "applies_when_json", "fails_when_json"))
        return extra, len(row["statement"])

    for row in rows:
        key = (row["source_id"], normalize_name(row["quote"]), row["metric_value"])
        if not key[1]:
            continue
        kept = best.get(key)
        if kept is None:
            best[key] = row
        elif richness(row) > richness(kept):
            pairs.append((row["id"], kept["id"]))
            best[key] = row
        else:
            pairs.append((kept["id"], row["id"]))
    # Si la que se quedaba fue sustituida después, se apunta a la definitiva.
    final = {(k[0], k[1], k[2]): v["id"] for k, v in best.items()}
    by_id = {r["id"]: (r["source_id"], normalize_name(r["quote"]), r["metric_value"]) for r in rows}
    return [(final[by_id[drop]], drop) for _, drop in pairs]


def remove_duplicate_claims(store: Store) -> int:
    """Marca como rechazadas las afirmaciones repetidas y pasa sus relaciones a la que se queda."""
    pairs = duplicate_claims(store)
    with store.db:
        for keep, drop in pairs:
            row = store.db.execute("SELECT attrs_json FROM claims WHERE id=?", (drop,)).fetchone()
            attrs = dict(json.loads(row["attrs_json"]), duplicado_de=keep)
            store.db.execute("UPDATE claims SET status='rejected', attrs_json=? WHERE id=?",
                             (json.dumps(attrs, ensure_ascii=False), drop))
            store.db.execute("UPDATE OR IGNORE claim_relations SET from_claim_id=? "
                             "WHERE from_claim_id=? AND to_claim_id != ?", (keep, drop, keep))
            store.db.execute("UPDATE OR IGNORE claim_relations SET to_claim_id=? "
                             "WHERE to_claim_id=? AND from_claim_id != ?", (keep, drop, keep))
            store.db.execute("DELETE FROM claim_relations WHERE from_claim_id=? OR to_claim_id=?", (drop, drop))
    return len(pairs)
