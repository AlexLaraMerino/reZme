"""Evaluación contra el conjunto de prueba (`evals/golden/`). Sin LLM.

Cada fichero describe las afirmaciones que se esperan de un vídeo. Se comparan
con la última extracción guardada de ese vídeo y se calculan:
  - recall: esperadas que aparecen entre las verificadas;
  - precisión: verificadas que corresponden a alguna esperada (solo es
    significativa si el fichero es exhaustivo);
  - grounding: verificadas / (verificadas + no ancladas);
  - exactitud numérica: emparejadas cuya cifra coincide con la esperada.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .schema import CLAIM_TYPES, ValidationError, normalize_name, parse_ts
from .store import Store

PLATFORM = "youtube"
NUMERIC_TOLERANCE = 0.005
DEFAULT_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "evals" / "golden"


def load_golden(path: str | Path) -> dict[str, Any]:
    """Lee y valida un fichero de expectativas."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path.name}: JSON no válido ({exc.msg}, línea {exc.lineno})") from None
    if not isinstance(data, dict) or not isinstance(data.get("video_id"), str):
        raise ValidationError(f"{path.name}: falta `video_id`")
    claims = data.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValidationError(f"{path.name}: falta la lista `claims`")
    seen: set[str] = set()
    for i, item in enumerate(claims):
        where = f"{path.name}: claims[{i}]"
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValidationError(f"{where}: falta `id`")
        if item["id"] in seen:
            raise ValidationError(f"{where}: id repetido {item['id']!r}")
        seen.add(item["id"])
        keywords = item.get("keywords")
        if not isinstance(keywords, list) or not keywords or not all(
                isinstance(k, str) or (isinstance(k, list) and k and all(
                    isinstance(a, str) for a in k)) for k in keywords):
            raise ValidationError(f"{where}: `keywords` debe ser una lista de textos o de alternativas")
        if item.get("type") is not None and item["type"] not in CLAIM_TYPES:
            raise ValidationError(f"{where}: type no válido {item['type']!r}")
        value = item.get("metric_value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValidationError(f"{where}: metric_value debe ser un número o null")
        if item.get("ts") is not None:
            parse_ts(item["ts"])
    return data


def _has(keyword: str, haystack: str) -> bool:
    """Palabra o frase completa; con `*` final, comienzo de palabra («vend*»)."""
    prefix = keyword.strip().endswith("*")
    norm = normalize_name(keyword)
    if not norm:
        return False
    return (f" {norm}" if prefix else f" {norm} ") in haystack


def _matches(expected: dict[str, Any], claim: dict[str, Any]) -> bool:
    haystack = f" {normalize_name(claim['statement'] + ' ' + (claim.get('quote') or ''))} "
    for keyword in expected["keywords"]:
        alternatives = [keyword] if isinstance(keyword, str) else keyword
        if not any(_has(a, haystack) for a in alternatives):
            return False
    return True


def _same_number(expected: float, actual: float | None) -> bool:
    return actual is not None and math.isclose(expected, actual, rel_tol=NUMERIC_TOLERANCE)


def _pick(expected: dict[str, Any], pool: list[dict[str, Any]], used: set[int]) -> dict[str, Any] | None:
    options = [c for c in pool if c["id"] not in used and _matches(expected, c)]
    if not options:
        return None
    value = expected.get("metric_value")
    ts = parse_ts(expected["ts"]) if expected.get("ts") is not None else None

    def rank(c: dict[str, Any]) -> tuple[int, float]:
        number_ok = value is not None and _same_number(value, c.get("metric_value"))
        distance = abs((c.get("ts_start") or 0.0) - ts) if ts is not None else 0.0
        return (0 if number_ok else 1, distance)

    return min(options, key=rank)


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def evaluate(store: Store, golden: dict[str, Any]) -> dict[str, Any]:
    """Métricas de un vídeo. `estado` = 'sin extraer' si aún no hay nada que comparar."""
    out: dict[str, Any] = {"video_id": golden["video_id"], "titulo": golden.get("title"),
                           "exhaustivo": bool(golden.get("exhaustive"))}
    source = store.get_source(PLATFORM, golden["video_id"])
    run = store.latest_run(source["id"]) if source else None
    if run is None:
        out["estado"] = "sin extraer"
        return out
    claims = store.claims_for_source(source["id"], run_id=run["id"])
    verified = [c for c in claims if c["status"] == "verified"]
    ungrounded = [c for c in claims if c["status"] == "ungrounded"]

    used: set[int] = set()
    missing, only_ungrounded, wrong_numbers = [], [], []
    numeric_total = numeric_ok = 0
    for expected in golden["claims"]:
        hit = _pick(expected, verified, used)
        if hit is None:
            missing.append(expected["id"])
            if _pick(expected, ungrounded, set()) is not None:
                only_ungrounded.append(expected["id"])
            continue
        used.add(hit["id"])
        if expected.get("metric_value") is not None:
            numeric_total += 1
            if _same_number(expected["metric_value"], hit.get("metric_value")):
                numeric_ok += 1
            else:
                wrong_numbers.append({"id": expected["id"], "esperado": expected["metric_value"],
                                      "extraido": hit.get("metric_value"), "claim_id": hit["id"]})
    out.update(
        estado="ok", run_id=run["id"], prompt_version=run["prompt_version"],
        backend=run["backend"], esperadas=len(golden["claims"]), verificadas=len(verified),
        no_ancladas=len(ungrounded), emparejadas=len(used),
        recall=_ratio(len(used), len(golden["claims"])),
        precision=_ratio(len(used), len(verified)),
        grounding=_ratio(len(verified), len(verified) + len(ungrounded)),
        exactitud_numerica=_ratio(numeric_ok, numeric_total),
        no_encontradas=missing, solo_sin_anclar=only_ungrounded, cifras_distintas=wrong_numbers)
    return out


def golden_files(path: str | Path | None = None) -> list[Path]:
    target = Path(path) if path else DEFAULT_GOLDEN_DIR
    if target.is_dir():
        return sorted(target.glob("*.json"))
    if target.is_file():
        return [target]
    raise ValueError(f"No existe {target}.")


def evaluate_path(store: Store, path: str | Path | None = None) -> list[dict[str, Any]]:
    return [evaluate(store, load_golden(f)) for f in golden_files(path)]
