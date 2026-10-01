"""CLI de reZme fase 2:  python -m rezme {ingest,extract,claims,eval,stats,search} ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .store import Store

DEFAULT_DB = Path(__file__).resolve().parent.parent / "rezme_data" / "rezme.db"


def db_path(arg: str | None) -> str:
    return arg or os.environ.get("REZME_DB") or str(DEFAULT_DB)


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import ingest_url

    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    with Store(db_path(args.db)) as store:
        try:
            res = ingest_url(store, args.url, langs, force=args.force, whisper=args.whisper,
                             whisper_model=args.whisper_model, cookies_from=args.cookies_from)
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    state = "ya estaba ingerido (sin descargar)" if res.skipped_download else (
        "transcripción nueva guardada" if res.new_transcript else "sin cambios en la transcripción")
    print(f"✓ {res.video_id}: {state} · origen={res.origin} · {res.n_cues} cues")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        print(json.dumps(store.stats(), indent=2, ensure_ascii=False))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        rows = store.search_claims(
            args.query, status=None if args.any_status else "verified", domain=args.domain,
            known_at=args.known_at, include_expired=args.include_expired, limit=args.limit)
    for r in rows:
        print(f"#{r['id']} [{r['status']}] {r['type']} · {r.get('entity_name') or '—'} · "
              f"{r['statement']}")
    if not rows:
        print("Sin resultados.", file=sys.stderr)
    return 0


def _hms(seconds: float | None) -> str:
    from .chunking import hms
    return hms(seconds) if seconds is not None else "--:--:--"


def _source_id(store: Store, ref: str) -> int | None:
    """Id de fuente a partir de un id numérico o de una URL ya ingerida."""
    from .ingest import PLATFORM, video_id_from_url

    if ref.isdigit():
        return int(ref) if store.get_source_by_id(int(ref)) else None
    video_id = video_id_from_url(ref)
    source = store.get_source(PLATFORM, video_id) if video_id else None
    return source["id"] if source else None


def cmd_extract(args: argparse.Namespace) -> int:
    from .backends import check_backend, make_backend
    from .extract import ExtractionError, extract_source
    from .ingest import ingest_url, video_id_from_url

    problem = check_backend(args.backend)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    with Store(db_path(args.db)) as store:
        source_id = _source_id(store, args.source)
        try:
            if source_id is None:
                if args.source.isdigit() or not video_id_from_url(args.source):
                    print("No existe esa fuente. Indica un id de fuente o una URL válida de YouTube.",
                          file=sys.stderr)
                    return 1
                print("→ Vídeo sin ingerir: descargando la transcripción…", file=sys.stderr)
                langs = [l.strip() for l in args.lang.split(",") if l.strip()]
                source_id = ingest_url(store, args.source, langs).source_id
            print(f"→ Extrayendo la fuente {source_id} con {args.backend}…", file=sys.stderr)
            res = extract_source(
                store, source_id, make_backend(args.backend, ollama_model=args.ollama_model),
                domain=None if args.domain == "auto" else args.domain, force=args.force,
                progress=lambda msg: print(msg, file=sys.stderr))
        except (ValueError, RuntimeError, ExtractionError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    origin = "run reutilizado" if res.reused_run else "run nuevo"
    print(f"✓ Fuente {source_id} · run {res.run_id} ({origin}, prompt {res.prompt_version}) · "
          f"{res.chunks_processed}/{res.chunks} tramos procesados, {res.chunks_skipped} ya hechos")
    print(f"  {res.claims_new} afirmaciones nuevas: {res.verified} verificadas, "
          f"{res.ungrounded} sin anclar · {res.implications} implicaciones · "
          f"{res.discarded} elementos descartados · {res.llm_calls} llamadas al modelo")
    if res.superseded:
        print(f"  {res.superseded} afirmaciones de extracciones anteriores pasan a «superseded».")
    if res.chunks_failed:
        print(f"  {res.chunks_failed} tramos fallaron; vuelve a lanzar el comando para reanudar.",
              file=sys.stderr)
        return 1
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        source_id = _source_id(store, args.source)
        if source_id is None:
            print("No existe esa fuente.", file=sys.stderr)
            return 1
        run_id = args.run
        if run_id is None and not args.all_runs:
            run = store.latest_run(source_id)
            run_id = run["id"] if run else None
        rows = store.claims_for_source(source_id, run_id=run_id, status=args.status)
        if args.json:
            for r in rows:
                r["implications"] = store.implications_for(r["id"])
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        for r in rows:
            metric = ""
            if r["metric_value"] is not None:
                metric = f" · {r['metric_value']:g} {r['metric_unit'] or ''}".rstrip()
            print(f"#{r['id']} [{r['status']}] {_hms(r['ts_start'])} {r['type']} · "
                  f"{r.get('entity_name') or '—'}{metric}")
            print(f"    {r['statement']}")
            if r["quote"]:
                print(f"    «{r['quote']}»")
            for reason in r["attrs"].get("grounding", {}).get("motivos", []):
                print(f"    ✗ {reason}")
            note = r["attrs"].get("entidad")
            if note:
                print(f"    ? entidad {note['nombre']!r}: {note['motivo']}")
            for i in store.implications_for(r["id"]):
                print(f"    → {i['target_label']}: {i['direction']} ({i['basis']})")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "sin afirmaciones"
        print(f"Fuente {source_id}" + (f" · run {run_id}" if run_id else "") + f": {summary}",
              file=sys.stderr)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import evaluate_path
    from .schema import ValidationError

    with Store(db_path(args.db)) as store:
        try:
            reports = evaluate_path(store, args.path)
        except (ValueError, ValidationError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
    else:
        pct = lambda v: "   —  " if v is None else f"{v:6.1%}"
        print(f"{'vídeo':<13}{'recall':>8}{'precis.':>9}{'ground.':>9}{'cifras':>8}  detalle")
        for r in reports:
            if r["estado"] != "ok":
                print(f"{r['video_id']:<13}{'sin extraer: lanza primero `rezme extract`':>44}")
                continue
            note = "" if r["exhaustivo"] else " (precisión orientativa: fichero no exhaustivo)"
            print(f"{r['video_id']:<13}{pct(r['recall']):>8}{pct(r['precision']):>9}"
                  f"{pct(r['grounding']):>9}{pct(r['exactitud_numerica']):>8}  "
                  f"{r['emparejadas']}/{r['esperadas']} esperadas, {r['verificadas']} verificadas, "
                  f"{r['no_ancladas']} sin anclar{note}")
            if r["no_encontradas"]:
                print(f"{'':13}no encontradas: {', '.join(r['no_encontradas'])}")
            for w in r["cifras_distintas"]:
                print(f"{'':13}cifra distinta en {w['id']}: esperado {w['esperado']}, "
                      f"extraído {w['extraido']} (#{w['claim_id']})")
    if not reports:
        print("No hay ficheros de expectativas.", file=sys.stderr)
        return 1
    return 0 if any(r["estado"] == "ok" for r in reports) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rezme", description="Base de conocimiento de reZme (fase 2).")
    p.add_argument("--db", help="Ruta de la base SQLite (o variable REZME_DB)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", parents=[common], help="Descarga y guarda la transcripción cruda de un vídeo")
    i.add_argument("url")
    i.add_argument("--lang", default="es,en")
    i.add_argument("--force", action="store_true", help="Volver a descargar aunque ya exista")
    i.add_argument("--whisper", action="store_true", help="Forzar transcripción con Whisper")
    i.add_argument("--whisper-model", default="small")
    i.add_argument("--cookies-from", metavar="NAVEGADOR")
    i.set_defaults(func=cmd_ingest)

    x = sub.add_parser("extract", parents=[common],
                       help="Extrae y verifica las afirmaciones de un vídeo")
    x.add_argument("source", metavar="url|source_id")
    x.add_argument("--backend", default="claude-code", choices=["claude-code", "api", "ollama"])
    x.add_argument("--ollama-model", default="qwen3:14b")
    x.add_argument("--domain", default="auto",
                   choices=["auto", "macro", "empresa", "ciencia", "cripto"],
                   help="Guía de dominio del prompt (auto = todas)")
    x.add_argument("--force", action="store_true",
                   help="Crear un run nuevo aunque ya exista uno igual")
    x.add_argument("--lang", default="es,en", help="Idiomas si hay que ingerir el vídeo")
    x.set_defaults(func=cmd_extract)

    c = sub.add_parser("claims", parents=[common],
                       help="Revisar lo extraído de una fuente, con su estado")
    c.add_argument("source", metavar="url|source_id")
    c.add_argument("--status", choices=["candidate", "verified", "ungrounded", "rejected",
                                        "superseded"])
    c.add_argument("--run", type=int, help="Run concreto (por defecto, el último)")
    c.add_argument("--all-runs", action="store_true", help="Incluir el histórico de runs")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_claims)

    e = sub.add_parser("eval", parents=[common],
                       help="Métricas contra el conjunto de prueba (evals/golden)")
    e.add_argument("path", nargs="?", help="Fichero o carpeta de expectativas")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_eval)

    s = sub.add_parser("stats", parents=[common], help="Recuento de registros")
    s.set_defaults(func=cmd_stats)

    q = sub.add_parser("search", parents=[common], help="Buscar afirmaciones (solo verificadas y vigentes)")
    q.add_argument("query")
    q.add_argument("--domain")
    q.add_argument("--known-at", help="Fecha ISO: lo que se sabía ese día")
    q.add_argument("--include-expired", action="store_true")
    q.add_argument("--any-status", action="store_true", help="Incluir no verificadas (depuración)")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(func=cmd_search)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
