"""Calibración: libro de previsiones y perfil de cada canal.

Una previsión solo sirve para puntuar a quien la hizo si en algún momento se
anota si acertó. Aquí se lleva ese libro:
  - cada previsión verificada tiene una fila con su fecha objetivo, cuando el
    horizonte que dijo el autor permite fijarla sin adivinar;
  - la resolución (acertó, falló, a medias, no evaluable) la decide una persona.
    Un modelo puede **sugerirla** a partir de afirmaciones posteriores de la
    propia base, citándolas, pero una sugerencia nunca cuenta como resolución;
  - con lo resuelto se calcula el perfil de cada canal, que los agentes pueden
    usar para ponderar.
"""
from __future__ import annotations

import calendar
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .backends import BackendUnavailable
from .extract import parse_json
from .schema import FORECAST_RESOLUTIONS, ValidationError, normalize_name, utc_now
from .store import Store

RESOLVED = ("correct", "incorrect", "partial")
_PROMPT = Path(__file__).resolve().parent / "prompts" / "previsiones.md"
# Marcas de horizonte abierto: «2027+», «más allá de 2027», «mid-2030s»… no dan una fecha.
_OPEN = re.compile(r"\+|\bbeyond\b|\bmas alla\b|\bafter\b|\ba partir\b|\bdesde\b|\d0s\b|\bmid\b|\bdecada\b")
_UNITS = {"dia": 1 / 30, "dias": 1 / 30, "day": 1 / 30, "days": 1 / 30, "semana": 0.25, "semanas": 0.25,
          "week": 0.25, "weeks": 0.25, "mes": 1, "meses": 1, "month": 1, "months": 1, "m": 1,
          "trimestre": 3, "trimestres": 3, "quarter": 3, "quarters": 3,
          "ano": 12, "anos": 12, "year": 12, "years": 12, "y": 12}
MAX_LATER_CLAIMS = 60


def _end_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _add_months(start: date, months: float) -> date:
    whole = int(months)
    total = start.month - 1 + whole
    year, month = start.year + total // 12, total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    out = date(year, month, day)
    extra = round((months - whole) * 30)
    return date.fromordinal(out.toordinal() + extra)


def target_date(horizon: str | None, published_at: str | None) -> str | None:
    """Fecha objetivo (AAAA-MM-DD) que se desprende del horizonte dicho, o None si no es precisa.

    «2027» → fin de 2027; «12 meses» → doce meses tras la publicación; «3 a 5 años» → el
    extremo largo. «largo plazo», «2027+» o «próximos años» no dan fecha: no se inventa.
    """
    if not horizon or not horizon.strip():
        return None
    text = normalize_name(horizon)
    raw = horizon.strip().lower()
    exact = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", raw)
    if exact:
        year, month = int(exact[1]), int(exact[2])
        if not 1 <= month <= 12:
            return None
        return (date(year, month, int(exact[3])) if exact[3] else _end_of_month(year, month)).isoformat()
    if _OPEN.search(raw) or _OPEN.search(text):
        return None
    year = re.search(r"\b(?:fy|fiscal ?)?(20\d\d)\b", text)
    if year:
        return date(int(year[1]), 12, 31).isoformat()
    published = None
    if published_at:
        try:
            published = date.fromisoformat(published_at[:10])
        except ValueError:
            published = None
    span = re.search(r"(?:(\d+(?:[.,]\d+)?)\s*(?:a|to|o|or|-)\s*)?(\d+(?:[.,]\d+)?)\s*([a-z]+)", text)
    if span and span[3] in _UNITS and published:
        amount = float(span[2].replace(",", "."))
        if 0 < amount <= 600:
            return _add_months(published, amount * _UNITS[span[3]]).isoformat()
    if published and text in ("este ano", "this year", "fin de ano", "end of year", "year end"):
        return date(published.year, 12, 31).isoformat()
    return None


def sync_forecasts(store: Store) -> int:
    """Crea la fila del libro para cada previsión verificada que aún no la tenga. Devuelve cuántas."""
    rows = store.db.execute(
        """SELECT c.id, c.horizon, c.valid_to, c.published_at FROM claims c
           LEFT JOIN forecasts f ON f.claim_id = c.id
           WHERE c.type = 'forecast' AND c.status = 'verified' AND f.id IS NULL""").fetchall()
    with store.db:
        for row in rows:
            target = (row["valid_to"] or "")[:10] or target_date(row["horizon"], row["published_at"])
            store.db.execute("INSERT INTO forecasts (claim_id, target_date, horizon_text) VALUES (?,?,?)",
                             (row["id"], target, row["horizon"]))
    return len(rows)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def list_forecasts(store: Store, today: str | None = None) -> list[dict[str, Any]]:
    """Previsiones verificadas con su estado en el libro. `due` si ya venció y sigue sin resolver."""
    today = today or _today()
    out = []
    for row in store.db.execute(
            """SELECT f.claim_id, f.target_date, f.resolution, f.resolved_at, f.notes, f.horizon_text,
                      f.evidence_json, f.suggestion_json, c.statement, c.published_at, c.source_id,
                      s.title AS source_title, s.channel, s.channel_id, e.canonical_name AS entity
               FROM forecasts f JOIN claims c ON c.id = f.claim_id JOIN sources s ON s.id = c.source_id
               LEFT JOIN entities e ON e.id = c.entity_id
               WHERE c.status = 'verified' AND c.type = 'forecast'
               ORDER BY f.target_date IS NULL, f.target_date, c.id"""):
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        raw = item.pop("suggestion_json")
        item["suggestion"] = json.loads(raw) if raw else None
        item["due"] = (item["resolution"] == "pending" and item["target_date"] is not None
                       and item["target_date"] <= today)
        out.append(item)
    return out


def resolve(store: Store, claim_id: int, resolution: str, *, notes: str | None = None,
            evidence: list[int] | None = None) -> None:
    """Anota el resultado de una previsión. `pending` la devuelve a sin resolver."""
    if resolution not in FORECAST_RESOLUTIONS:
        raise ValidationError(f"resolución no válida: {resolution!r} (permitidas: {', '.join(FORECAST_RESOLUTIONS)})")
    ids = [i for i in dict.fromkeys(evidence or []) if isinstance(i, int) and not isinstance(i, bool)]
    with store.db:
        cur = store.db.execute(
            """UPDATE forecasts SET resolution=?, resolved_at=?, notes=?, evidence_json=?, suggestion_json=NULL
               WHERE claim_id=?""",
            (resolution, None if resolution == "pending" else utc_now(), (notes or "").strip()[:1000] or None,
             json.dumps(ids), claim_id))
    if cur.rowcount == 0:
        raise KeyError(f"la previsión {claim_id} no está en el libro")


def _later_claims(store: Store, entity_id: int, after: str) -> list[Any]:
    return store.db.execute(
        """SELECT c.id, c.type, c.statement, c.published_at, s.channel FROM claims c
           JOIN sources s ON s.id = c.source_id
           WHERE c.entity_id = ? AND c.status = 'verified' AND c.type != 'forecast'
             AND c.published_at > ? ORDER BY c.published_at DESC, c.id DESC LIMIT ?""",
        (entity_id, after, MAX_LATER_CLAIMS)).fetchall()


def suggestion_batches(store: Store) -> list[dict[str, Any]]:
    """Por entidad: previsiones sin resolver ni sugerencia y afirmaciones posteriores que podrían resolverlas."""
    pending = store.db.execute(
        """SELECT f.claim_id, c.statement, c.published_at, c.entity_id, f.target_date, f.horizon_text,
                  e.canonical_name FROM forecasts f JOIN claims c ON c.id = f.claim_id
           JOIN entities e ON e.id = c.entity_id
           WHERE f.resolution = 'pending' AND f.suggestion_json IS NULL AND c.status = 'verified'
             AND c.published_at IS NOT NULL ORDER BY c.entity_id, c.id""").fetchall()
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in pending:
        grouped[row["entity_id"]].append(row)
    batches = []
    for entity_id, forecasts in grouped.items():
        earliest = min(f["published_at"] for f in forecasts)
        later = _later_claims(store, entity_id, earliest)
        usable = [f for f in forecasts if any(c["published_at"] > f["published_at"] for c in later)]
        if usable:
            batches.append({"entity": forecasts[0]["canonical_name"], "forecasts": usable, "claims": later})
    return batches


def suggest(store: Store, call: Callable[[str, str], str], *, workers: int = 1,
            progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """Pide al modelo sugerencias de resolución apoyadas en afirmaciones posteriores de la base.

    Solo se guarda una sugerencia si cita al menos una afirmación posterior a la previsión y de
    la lista enviada. Nunca cambia la resolución: eso lo hace `resolve`, tras revisarla alguien.
    """
    say = progress or (lambda message: None)
    system = _PROMPT.read_text(encoding="utf-8").strip()
    batches = suggestion_batches(store)
    result = {"entidades": len(batches), "sugerencias": 0}

    def ask(batch: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
        forecasts = "\n".join(json.dumps({
            "id": f["claim_id"], "fecha": f["published_at"][:10], "horizonte": f["horizon_text"],
            "vence": f["target_date"], "texto": f["statement"]}, ensure_ascii=False) for f in batch["forecasts"])
        claims = "\n".join(json.dumps({
            "id": c["id"], "fecha": c["published_at"][:10], "tipo": c["type"], "canal": c["channel"],
            "texto": c["statement"]}, ensure_ascii=False) for c in batch["claims"])
        answer = call(system, f"Entidad: {batch['entity']}\n<previsiones>\n{forecasts}\n</previsiones>\n"
                              f"<afirmaciones_posteriores>\n{claims}\n</afirmaciones_posteriores>")
        try:
            data = parse_json(answer)
        except ValidationError:
            return []
        published = {f["claim_id"]: f["published_at"] for f in batch["forecasts"]}
        dated = {c["id"]: c["published_at"] for c in batch["claims"]}
        found = []
        for raw in data.get("resolutions", []) if isinstance(data, dict) else []:
            if not isinstance(raw, dict) or raw.get("id") not in published or raw.get("resolution") not in RESOLVED:
                continue
            evidence = [i for i in raw.get("evidence") or [] if isinstance(i, int) and not isinstance(i, bool)
                        and i in dated and dated[i] > published[raw["id"]]]
            if not evidence:
                continue  # sin prueba posterior en la base, no hay sugerencia
            reason = raw.get("reason")
            found.append((raw["id"], {"resolution": raw["resolution"], "evidence": list(dict.fromkeys(evidence)),
                                      "reason": reason[:300] if isinstance(reason, str) else ""}))
        return found

    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches) or 1)))
    try:
        futures = [(batch, pool.submit(ask, batch)) for batch in batches]
        for position, (batch, future) in enumerate(futures, 1):
            say(f"Revisando previsiones sobre «{batch['entity']}» ({position} de {len(futures)})")
            try:
                suggestions = future.result()
            except BackendUnavailable:
                raise
            except Exception:
                continue
            with store.db:
                for claim_id, suggestion in suggestions:
                    result["sugerencias"] += store.db.execute(
                        "UPDATE forecasts SET suggestion_json=? WHERE claim_id=? AND resolution='pending'",
                        (json.dumps(suggestion, ensure_ascii=False), claim_id)).rowcount
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return result


def channel_profiles(store: Store, today: str | None = None) -> list[dict[str, Any]]:
    """Perfil de cada canal, calculado con lo que hay en la base, y guardado en `source_profiles`."""
    today = today or _today()
    profiles: dict[str, dict[str, Any]] = {}
    for row in store.db.execute(
            "SELECT IFNULL(channel_id, 's' || id) AS key, channel, platform, COUNT(*) AS videos, "
            "MIN(published_at) AS first, MAX(published_at) AS last FROM sources GROUP BY key"):
        profiles[row["key"]] = {
            "channel_id": row["key"], "channel": row["channel"] or "(sin canal)", "platform": row["platform"],
            "videos": row["videos"], "first": row["first"], "last": row["last"], "verified": 0, "ungrounded": 0,
            "durable": 0, "forecasts": 0, "pending": 0, "due": 0, "correct": 0, "incorrect": 0, "partial": 0,
            "void": 0, "supported": 0, "contradicted": 0}
    for row in store.db.execute(
            """SELECT IFNULL(s.channel_id, 's' || s.id) AS key, c.status, c.type, COUNT(*) AS n
               FROM claims c JOIN sources s ON s.id = c.source_id
               WHERE c.status IN ('verified', 'ungrounded') GROUP BY key, c.status, c.type"""):
        profile = profiles[row["key"]]
        profile[row["status"]] += row["n"]
        if row["status"] == "verified" and row["type"] in ("mechanism", "mental_model", "heuristic", "framework",
                                                            "causal_claim", "historical_case"):
            profile["durable"] += row["n"]
    for item in list_forecasts(store, today):
        profile = profiles[item["channel_id"] or f"s{item['source_id']}"]
        profile["forecasts"] += 1
        profile[item["resolution"]] += 1
        profile["due"] += item["due"]
    for relation, field in (("supports", "supported"), ("contradicts", "contradicted")):
        for row in store.db.execute(
                """SELECT key, COUNT(DISTINCT claim) AS n FROM (
                       SELECT IFNULL(sa.channel_id, 's' || sa.id) AS key, a.id AS claim
                       FROM claim_relations r JOIN claims a ON a.id = r.from_claim_id
                       JOIN sources sa ON sa.id = a.source_id JOIN claims b ON b.id = r.to_claim_id
                       JOIN sources sb ON sb.id = b.source_id
                       WHERE r.relation = ? AND a.status = 'verified' AND b.status = 'verified'
                         AND IFNULL(sa.channel_id, 's' || sa.id) != IFNULL(sb.channel_id, 's' || sb.id)
                       UNION
                       SELECT IFNULL(sb.channel_id, 's' || sb.id), b.id
                       FROM claim_relations r JOIN claims a ON a.id = r.from_claim_id
                       JOIN sources sa ON sa.id = a.source_id JOIN claims b ON b.id = r.to_claim_id
                       JOIN sources sb ON sb.id = b.source_id
                       WHERE r.relation = ? AND a.status = 'verified' AND b.status = 'verified'
                         AND IFNULL(sa.channel_id, 's' || sa.id) != IFNULL(sb.channel_id, 's' || sb.id))
                   GROUP BY key""", (relation, relation)):
            profiles[row["key"]][field] = row["n"]
    out = []
    for profile in profiles.values():
        resolved = profile["correct"] + profile["incorrect"] + profile["partial"]
        profile["resolved"] = resolved
        # Acierto: una previsión a medias cuenta la mitad. None mientras no haya nada resuelto.
        profile["hit_rate"] = round((profile["correct"] + 0.5 * profile["partial"]) / resolved, 3) if resolved else None
        checked = profile["verified"] + profile["ungrounded"]
        profile["grounding"] = round(profile["verified"] / checked, 3) if checked else None
        out.append(profile)
    out.sort(key=lambda p: -p["videos"])
    if not store.readonly:
        with store.db:
            for profile in out:
                store.db.execute(
                    """INSERT INTO source_profiles (platform, channel_id, metrics_json, updated_at)
                       VALUES (?,?,?,?) ON CONFLICT(platform, channel_id)
                       DO UPDATE SET metrics_json=excluded.metrics_json, updated_at=excluded.updated_at""",
                    (profile["platform"], profile["channel_id"], json.dumps(profile, ensure_ascii=False), utc_now()))
    return out
