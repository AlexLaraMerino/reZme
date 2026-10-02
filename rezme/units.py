"""Normalización determinista de cifras: «45 miles de millones USD» -> 4,5e10 USD.

El extractor guarda la cifra tal como se dice y deja la escala en el texto de la
unidad, que es lo que permite verificarla contra la transcripción. Para comparar
cifras entre vídeos hace falta además un valor absoluto y una unidad canónica;
eso es lo que calcula `normalize`. Lo que no se entiende con seguridad se deja
sin normalizar: mejor ningún valor que uno equivocado.
"""
from __future__ import annotations

import re
import unicodedata

# Escalas dichas con palabras al principio de la unidad. «billón» (es) = 10^12, «billion» (en) = 10^9.
_SCALES = (
    ("miles de millones", 1e9), ("mil millones", 1e9), ("millones", 1e6), ("millon", 1e6),
    ("billones", 1e12), ("billon", 1e12), ("miles", 1e3), ("mil", 1e3),
    ("thousands", 1e3), ("thousand", 1e3), ("millions", 1e6), ("million", 1e6),
    ("billions", 1e9), ("billion", 1e9), ("trillions", 1e12), ("trillion", 1e12),
)
# «trillones» suele ser un calco del inglés (10^12) y no el trillón español (10^18): ambiguo.
_AMBIGUOUS = ("trillones", "trillon")
_CURRENCIES = {
    "usd": "USD", "$": "USD", "dolares": "USD", "dolar": "USD", "us$": "USD",
    "eur": "EUR", "euros": "EUR", "euro": "EUR", "€": "EUR",
    "yuanes": "CNY", "yuan": "CNY", "cny": "CNY", "jpy": "JPY", "yenes": "JPY", "krw": "KRW",
    "cad": "CAD", "gbp": "GBP", "libras": "GBP", "btc": "BTC",
}
_PLAIN = {
    "%": "%", "por ciento": "%", "percent": "%", "puntos basicos": "pb", "puntos porcentuales": "pp",
    "pp": "pp", "x": "x", "veces": "x", "multiplo": "x", "multiplicador": "x", "ratio": "x",
    "count": "", "unidades": "", "": "",
}
# Unidades físicas con prefijo: se distinguen mayúsculas (mW no es MW).
_SI_EXACT = {
    "W": (1, "W"), "mW": (1e-3, "W"), "kW": (1e3, "W"), "MW": (1e6, "W"), "GW": (1e9, "W"), "TW": (1e12, "W"),
    "Wh": (1, "Wh"), "kWh": (1e3, "Wh"), "MWh": (1e6, "Wh"), "GWh": (1e9, "Wh"), "TWh": (1e12, "Wh"),
    "Hz": (1, "Hz"), "kHz": (1e3, "Hz"), "MHz": (1e6, "Hz"), "GHz": (1e9, "Hz"),
    "bps": (1, "bps"), "kbps": (1e3, "bps"), "Mbps": (1e6, "bps"), "Gbps": (1e9, "bps"), "Tbps": (1e12, "bps"),
    "Gbit/s": (1e9, "bps"), "Mbit/s": (1e6, "bps"),
    "B": (1, "B"), "kB": (1e3, "B"), "MB": (1e6, "B"), "GB": (1e9, "B"), "TB": (1e12, "B"),
    "m": (1, "m"), "km": (1e3, "m"), "cm": (1e-2, "m"), "mm": (1e-3, "m"), "nm": (1e-9, "m"),
    "g": (1, "g"), "kg": (1e3, "g"), "s": (1, "s"), "ms": (1e-3, "s"),
}
_SI_WORDS = {
    "vatios": (1, "W"), "watt": (1, "W"), "watts": (1, "W"), "kilovatios": (1e3, "W"),
    "megawatt": (1e6, "W"), "megavatios": (1e6, "W"), "gigavatios": (1e9, "W"), "gigawatt": (1e9, "W"),
    "megabytes": (1e6, "B"), "gigabytes": (1e9, "B"), "terabytes": (1e12, "B"),
    "gigabits per second": (1e9, "bps"), "terabits per second": (1e12, "bps"),
    "nanometros": (1e-9, "m"), "kilometros": (1e3, "m"), "metros": (1, "m"), "meter": (1, "m"),
    "toneladas": (1e6, "g"), "toneladas metricas": (1e6, "g"), "metric tons": (1e6, "g"),
    "segundos": (1, "s"), "minutos": (60, "s"), "minutes": (60, "s"),
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _base(rest: str) -> str:
    """Unidad canónica de lo que queda tras quitar la escala."""
    rest = rest.strip()
    folded = _fold(rest)
    if folded in _PLAIN:
        return _PLAIN[folded]
    if folded in _CURRENCIES:
        return _CURRENCIES[folded]
    # Compuestas: «USD por acción», «$/MWh», «USD per share».
    parts = re.split(r"\s*(?:/|\bpor\b|\bper\b)\s*", rest, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        head = _CURRENCIES.get(_fold(parts[0]), parts[0].strip())
        return f"{head}/{parts[1].strip()}"
    return rest  # se respeta la escritura: «dB» no es «db»


def parse_unit(unit: str | None) -> tuple[float, str] | None:
    """(factor, unidad canónica) de una unidad tal como la escribió el extractor. None si es ambigua."""
    text = " ".join((unit or "").split())
    if text in _SI_EXACT:
        return _SI_EXACT[text]
    folded = _fold(text)
    if folded in _SI_WORDS:
        return _SI_WORDS[folded]
    if any(folded == word or folded.startswith(word + " ") for word in _AMBIGUOUS):
        return None
    for words, factor in _SCALES:
        if folded == words:
            return factor, ""
        if folded.startswith(words + " "):
            rest = text[len(words):].strip()
            rest = re.sub(r"^de\s+", "", rest, flags=re.IGNORECASE)
            if "(" in rest:  # «billones USD (trillones US)»: el propio texto duda de la escala
                return None
            return factor, _base(rest)
    return 1.0, _base(text)


def normalize(value: float | None, unit: str | None) -> tuple[float | None, str | None]:
    """(valor absoluto, unidad canónica). (None, None) si no hay cifra o la escala es ambigua."""
    if value is None:
        return None, None
    parsed = parse_unit(unit)
    if parsed is None:
        return None, None
    factor, base = parsed
    return value * factor, base
