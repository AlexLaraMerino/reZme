"""Backends de modelo para la extracción.

`api` y `ollama` reutilizan los de `yt_digest` sin modificarlos: esas funciones
llevan fijo el prompt de sistema del informe, así que se sustituye
`yt_digest.SYSTEM` solo mientras dura la llamada, y no informan del coste.

`claude-code` tiene llamada propia: la de `yt_digest` usa `--bare`, que en las
versiones actuales del CLI ignora la sesión iniciada y exige una clave de API.
Aquí se llama sin `--bare`, con las herramientas desactivadas (la transcripción
es un dato no confiable) y con salida JSON para registrar el coste.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

import yt_digest

BACKENDS = ("claude-code", "api", "ollama")
DEFAULT_BACKEND = "claude-code"
MAX_TOKENS = 8000
CLAUDE_TIMEOUT_S = 900

class BackendUnavailable(RuntimeError):
    """El modelo no se puede usar (credenciales, saldo, modelo inexistente): no tiene sentido seguir."""


_ACCESS_HINTS = ("log in", "login", "authenticat", "api key", "credit", "usage limit",
                 "invalid x-api-key", "unauthorized")

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


def run_claude_code(system: str, user: str) -> tuple[str, float | None]:
    """Una llamada al CLI `claude` en modo print. Devuelve (texto, coste en USD si se informa)."""
    cmd = ["claude", "-p", "--system-prompt", system, "--tools", "", "--output-format", "json",
           "--no-session-persistence"]
    try:
        # Directorio neutro: que no arrastre CLAUDE.md ni ajustes del proyecto.
        proc = subprocess.run(cmd, input=user, text=True, capture_output=True,
                              cwd=tempfile.gettempdir(), timeout=CLAUDE_TIMEOUT_S)
    except FileNotFoundError:
        raise BackendUnavailable("No encuentro el CLI `claude` en el PATH.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude no respondió a tiempo (timed out)") from None
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:300]
        if any(hint in detail.lower() for hint in _ACCESS_HINTS):
            raise BackendUnavailable(f"Claude Code no está disponible: {detail}")
        raise RuntimeError(f"claude falló ({proc.returncode}): {detail}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout.strip(), None
    if not isinstance(data, dict):
        return proc.stdout.strip(), None
    if data.get("is_error"):
        detail = str(data.get("result"))[:300]
        if any(hint in detail.lower() for hint in _ACCESS_HINTS):
            raise BackendUnavailable(f"Claude Code no está disponible: {detail}")
        raise RuntimeError(f"claude devolvió un error: {detail}")
    cost = data.get("total_cost_usd")
    return str(data.get("result") or "").strip(), cost if isinstance(cost, (int, float)) else None


def make_backend(name: str = DEFAULT_BACKEND, *, ollama_model: str = "qwen3:14b") -> Backend:
    if name == "claude-code":
        backend = Backend(name, "predeterminado del CLI", lambda system, user: "")

        def call(system: str, user: str) -> str:
            text, cost = run_claude_code(system, user)
            if cost is not None:
                backend.cost_usd = (backend.cost_usd or 0.0) + cost
            return text
        backend.call = call
        return backend
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
