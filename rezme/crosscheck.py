"""Contraste entre vídeos: qué afirmaciones se corroboran y cuáles se contradicen.

Dentro de un vídeo el extractor ya enlaza las afirmaciones entre sí. Aquí se
cruzan las de **fuentes distintas** sobre una misma entidad, que es lo que
permite saber cuántos canales independientes sostienen algo. Dos vídeos del
mismo canal no cuentan como corroboración independiente.

Al modelo se le dan las afirmaciones de cada entidad compartida, etiquetadas por
canal, y devuelve parejas que se apoyan, se contradicen o se matizan. Solo se
aceptan parejas de canales distintos y con ids de la lista enviada.

No hay regla automática de «misma cifra = apoyo»: probada sobre datos reales
emparejaba un «5 %» de 2007 con un «5 %» de hoy. Un apoyo falso infla la
corroboración, que es justo lo que un agente usará para fiarse.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .backends import BackendUnavailable
from .extract import parse_json
from .schema import ValidationError
from .store import Store

CROSS_RELATIONS = ("supports", "contradicts", "refines")
MAX_CLAIMS_PER_CALL = 140
_PROMPT = Path(__file__).resolve().parent / "prompts" / "contraste.md"


def _channel(row: Any) -> str:
    return row["channel_id"] or f"s{row['source_id']}"


def shared_entities(store: Store) -> list[dict[str, Any]]:
    """Entidades con afirmaciones verificadas de al menos dos canales, con esas afirmaciones."""
    rows = store.db.execute(
        """SELECT c.id, c.entity_id, c.type, c.statement, c.metric_name, c.metric_value_abs,
                  c.metric_unit_base, c.as_of, c.published_at, c.source_id, s.channel_id, s.channel,
                  e.canonical_name
           FROM claims c JOIN sources s ON s.id = c.source_id JOIN entities e ON e.id = c.entity_id
           WHERE c.status = 'verified' ORDER BY c.entity_id, c.id""").fetchall()
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row["entity_id"]].append(row)
    out = []
    for entity_id, claims in grouped.items():
        if len({_channel(c) for c in claims}) >= 2:
            digest = hashlib.sha1(",".join(str(c["id"]) for c in claims).encode()).hexdigest()
            out.append({"id": entity_id, "name": claims[0]["canonical_name"], "claims": claims,
                        "hash": digest})
    return out


def _batches(entity: dict[str, Any]) -> list[list[Any]]:
    """Trozos de como mucho MAX_CLAIMS_PER_CALL afirmaciones, cada uno con al menos dos canales."""
    claims = entity["claims"]
    if len(claims) <= MAX_CLAIMS_PER_CALL:
        return [claims]
    by_channel: dict[str, list[Any]] = defaultdict(list)
    for claim in claims:
        by_channel[_channel(claim)].append(claim)
    biggest = max(by_channel, key=lambda k: len(by_channel[k]))
    others = [c for k, v in by_channel.items() if k != biggest for c in v][:MAX_CLAIMS_PER_CALL // 2]
    room = MAX_CLAIMS_PER_CALL - len(others)
    big = by_channel[biggest]
    return [others + big[i:i + room] for i in range(0, len(big), room)]


def _lines(claims: list[Any]) -> tuple[str, dict[str, str]]:
    letters: dict[str, str] = {}
    lines = []
    for claim in claims:
        letter = letters.setdefault(_channel(claim), chr(ord("A") + len(letters)))
        item = {"id": claim["id"], "canal": letter, "tipo": claim["type"],
                "fecha": (claim["as_of"] or claim["published_at"] or "")[:10], "texto": claim["statement"]}
        if claim["metric_value_abs"] is not None:
            item["cifra"] = f"{claim['metric_value_abs']:g} {claim['metric_unit_base'] or ''}".strip()
        lines.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(lines), letters


def _ask(call: Callable[[str, str], str], system: str, entity: dict[str, Any],
         claims: list[Any]) -> list[tuple[int, int, str, str]]:
    text, _ = _lines(claims)
    answer = call(system, f"Entidad: {entity['name']}\n<afirmaciones>\n{text}\n</afirmaciones>")
    try:
        data = parse_json(answer)
    except ValidationError:
        return []
    by_id = {c["id"]: c for c in claims}
    found = []
    for raw in data.get("relations", []) if isinstance(data, dict) else []:
        if not isinstance(raw, dict):
            continue
        a, b, relation = raw.get("a"), raw.get("b"), raw.get("relation")
        valid_ids = all(isinstance(x, int) and not isinstance(x, bool) and x in by_id for x in (a, b))
        if not valid_ids or a == b or relation not in CROSS_RELATIONS:
            continue
        if _channel(by_id[a]) == _channel(by_id[b]):
            continue  # mismo canal: no es contraste entre fuentes independientes
        reason = raw.get("reason")
        found.append((a, b, relation, reason[:240] if isinstance(reason, str) else ""))
    return found


def cross_check(store: Store, call: Callable[[str, str], str], *, workers: int = 1,
                force: bool = False, progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """Cruza las afirmaciones de las entidades compartidas entre canales. Devuelve el recuento.

    Las entidades ya contrastadas con las mismas afirmaciones se saltan, salvo `force`.
    """
    say = progress or (lambda message: None)
    done = {r["entity_id"]: r["claims_hash"] for r in store.db.execute("SELECT * FROM cross_checks")}
    entities = shared_entities(store)
    todo = [e for e in entities if force or done.get(e["id"]) != e["hash"]]
    result = {"entidades": len(entities), "revisadas": 0, "supports": 0, "contradicts": 0,
              "refines": 0}
    system = _PROMPT.read_text(encoding="utf-8").strip()
    jobs = [(entity, batch) for entity in todo for batch in _batches(entity)]
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs) or 1)))
    try:
        futures = [(entity, pool.submit(_ask, call, system, entity, batch)) for entity, batch in jobs]
        failed: set[int] = set()
        for position, (entity, future) in enumerate(futures, 1):
            say(f"Contrastando «{entity['name']}» ({position} de {len(futures)})")
            try:
                relations = future.result()
            except BackendUnavailable:
                raise
            except Exception:
                failed.add(entity["id"])  # se reintentará en la próxima pasada
                continue
            for a, b, relation, reason in relations:
                result[relation] += store.add_relation(a, relation, b, reason=reason or None)
        for entity in todo:
            if entity["id"] not in failed:
                with store.db:
                    store.db.execute(
                        "INSERT OR REPLACE INTO cross_checks (entity_id, claims_hash, checked_at) "
                        "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))", (entity["id"], entity["hash"]))
                result["revisadas"] += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return result


def cross_relations(store: Store, relation: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    """Relaciones entre afirmaciones verificadas de canales distintos, con sus dos lados."""
    sql = """SELECT r.id, r.relation, r.reason,
                    a.id AS a_id, a.statement AS a_statement, sa.title AS a_title, sa.channel AS a_channel,
                    a.source_id AS a_source,
                    b.id AS b_id, b.statement AS b_statement, sb.title AS b_title, sb.channel AS b_channel,
                    b.source_id AS b_source, e.canonical_name AS entity
             FROM claim_relations r
             JOIN claims a ON a.id = r.from_claim_id JOIN sources sa ON sa.id = a.source_id
             JOIN claims b ON b.id = r.to_claim_id JOIN sources sb ON sb.id = b.source_id
             LEFT JOIN entities e ON e.id = a.entity_id
             WHERE a.status = 'verified' AND b.status = 'verified'
               AND IFNULL(sa.channel_id, 's' || sa.id) != IFNULL(sb.channel_id, 's' || sb.id)"""
    params: list[Any] = []
    if relation:
        sql += " AND r.relation = ?"
        params.append(relation)
    return [dict(r) for r in store.db.execute(sql + " ORDER BY r.id DESC LIMIT ?", (*params, limit))]


def summary(store: Store) -> dict[str, int]:
    counts = {r["relation"]: r["n"] for r in store.db.execute(
        """SELECT r.relation, COUNT(*) AS n FROM claim_relations r
           JOIN claims a ON a.id = r.from_claim_id JOIN sources sa ON sa.id = a.source_id
           JOIN claims b ON b.id = r.to_claim_id JOIN sources sb ON sb.id = b.source_id
           WHERE a.status = 'verified' AND b.status = 'verified'
             AND IFNULL(sa.channel_id, 's' || sa.id) != IFNULL(sb.channel_id, 's' || sb.id)
           GROUP BY r.relation""")}
    entities = shared_entities(store)
    checked = {r["entity_id"]: r["claims_hash"] for r in store.db.execute("SELECT * FROM cross_checks")}
    return {"canales": store.db.execute("SELECT COUNT(DISTINCT IFNULL(channel_id, 's' || id)) FROM sources").fetchone()[0],
            "entidades": len(entities),
            "pendientes": sum(1 for e in entities if checked.get(e["id"]) != e["hash"]),
            "llamadas": sum(len(_batches(e)) for e in entities if checked.get(e["id"]) != e["hash"]),
            "apoyos": counts.get("supports", 0), "contradicciones": counts.get("contradicts", 0),
            "matices": counts.get("refines", 0)}
