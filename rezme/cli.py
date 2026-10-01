"""CLI de reZme fase 2:  python -m rezme {ingest,stats,search} ..."""
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
