"""Prompts versionados de la extracción. Cada versión es un directorio (v1, v2…)."""
from __future__ import annotations

from pathlib import Path

from ..schema import (
    CLAIM_TYPES, DECISION_TAGS, DIRECTIONS, ENTITY_TYPES, EVIDENCE_GRADES, IMPLICATION_BASES,
    RELATION_TYPES, STANCES,
)

PROMPT_VERSION = "v3"
DOMAINS = ("macro", "empresa", "ciencia", "cripto")
_DIR = Path(__file__).resolve().parent


def fill(template: str, **values: object) -> str:
    """Sustituye {{CLAVE}} una sola vez por clave, sin reinterpretar lo insertado."""
    out = []
    for i, part in enumerate(template.split("{{")):
        if i == 0:
            out.append(part)
            continue
        key, sep, rest = part.partition("}}")
        out.append(str(values[key]) + rest if sep and key in values else "{{" + part)
    return "".join(out)


def load(name: str, version: str = PROMPT_VERSION) -> str:
    path = _DIR / version / f"{name}.md"
    if not path.is_file():
        raise ValueError(f"No existe el prompt {name!r} en la versión {version!r}.")
    return path.read_text(encoding="utf-8").strip()


def system_prompt(domain: str | None = None, version: str = PROMPT_VERSION) -> str:
    """Prompt de sistema; con `domain` solo incluye la guía de ese dominio."""
    if domain is not None and domain not in DOMAINS:
        raise ValueError(f"Dominio no válido: {domain!r} (permitidos: {', '.join(DOMAINS)}).")
    guides = [load(f"dominio_{d}", version) for d in ([domain] if domain else DOMAINS)]
    return fill(
        load("system", version),
        CLAIM_TYPES=", ".join(CLAIM_TYPES), EVIDENCE_GRADES=", ".join(EVIDENCE_GRADES),
        STANCES=", ".join(STANCES), ENTITY_TYPES=", ".join(ENTITY_TYPES),
        DIRECTIONS=", ".join(DIRECTIONS), IMPLICATION_BASES=", ".join(IMPLICATION_BASES),
        DECISION_TAGS=", ".join(DECISION_TAGS), RELATION_TYPES=", ".join(RELATION_TYPES),
        GUIA_DOMINIO="## Guía por dominio\n\n" + "\n\n".join(guides))
