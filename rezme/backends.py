"""Backends de modelo para la extracción.

Reutiliza los de `yt_digest` (claude-code, api, ollama) sin modificarlos. Esas
funciones llevan fijo el prompt de sistema del informe en Markdown, así que se
sustituye `yt_digest.SYSTEM` solo mientras dura la llamada. Como no devuelven
consumo, el coste queda sin registrar (`cost_usd` nulo).
"""
from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

import yt_digest

BACKENDS = ("claude-code", "api", "ollama")
DEFAULT_BACKEND = "claude-code"
MAX_TOKENS = 8000

# (system, user) -> texto de la respuesta
LLMCall = Callable[[str, str], str]

_lock = threading.Lock()


@dataclass
class Backend:
    name: str
    model: str | None
    call: LLMCall
    cost_usd: float | None = None  # acumulado, si el backend lo informa


@contextmanager
def _system(system: str) -> Iterator[None]:
    with _lock:
        original = yt_digest.SYSTEM
        yt_digest.SYSTEM = system
        try:
            yield
        finally:
            yt_digest.SYSTEM = original


def make_backend(name: str = DEFAULT_BACKEND, *, ollama_model: str = "qwen3:14b") -> Backend:
    if name == "claude-code":
        def call(system: str, user: str) -> str:
            with _system(system):
                return yt_digest.run_claude_code(user, MAX_TOKENS)
        return Backend(name, "predeterminado del CLI", call)
    if name == "api":
        def call(system: str, user: str) -> str:
            with _system(system):
                return yt_digest.run_api(user, MAX_TOKENS)
        return Backend(name, yt_digest.API_MODEL, call)
    if name == "ollama":
        def call(system: str, user: str) -> str:
            with _system(system):
                return yt_digest.run_ollama(user, MAX_TOKENS, ollama_model)
        return Backend(name, ollama_model, call)
    raise ValueError(f"Backend no válido: {name!r} (permitidos: {', '.join(BACKENDS)}).")


def check_backend(name: str) -> str | None:
    """Mensaje de error si falta algo para usar el backend; None si está listo."""
    if name == "claude-code" and not shutil.which("claude"):
        return "No encuentro el CLI `claude` en el PATH."
    if name == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return "Falta la variable de entorno ANTHROPIC_API_KEY."
        if not yt_digest.module_available("anthropic"):
            return "Falta el paquete Python `anthropic`. Instálalo con: pip install anthropic"
    return None
