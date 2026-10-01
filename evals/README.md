# Conjunto de prueba (golden set)

Cada fichero de `golden/` describe las afirmaciones clave que se esperan de un
vídeo. `python -m rezme eval` las compara con la última extracción guardada de
ese vídeo (no llama a ningún modelo) y calcula:

| Métrica | Definición |
|---|---|
| recall | esperadas que aparecen entre las afirmaciones `verified` |
| precisión | `verified` que corresponden a alguna esperada. Solo es significativa si el fichero tiene `"exhaustive": true`; si no, es orientativa |
| grounding | `verified` / (`verified` + `ungrounded`) |
| exactitud numérica | emparejadas con `metric_value` esperado cuya cifra coincide (tolerancia 0,5 %) |

Uso:

    python -m rezme extract <url>
    python -m rezme eval                      # toda la carpeta evals/golden
    python -m rezme eval evals/golden/fYoi6OjmIlw.json --json

## Formato

```json
{
  "video_id": "fYoi6OjmIlw",
  "url": "https://www.youtube.com/watch?v=fYoi6OjmIlw",
  "title": "…",
  "status": "borrador_sin_revisar",
  "exhaustive": false,
  "notes": "…",
  "claims": [
    {
      "id": "asts-01",
      "statement": "Qué se espera, en una frase (solo para quien lee).",
      "type": "own_calculation",
      "keywords": ["eficiencia espectral", ["bps", "bits por segundo"]],
      "metric_value": 1.36,
      "metric_unit": "bps/Hz",
      "ts": "02:01:14"
    }
  ]
}
```

- `keywords`: una afirmación extraída casa con la esperada si contiene **todas**
  las entradas (en su `statement` o en su `quote`, sin distinguir tildes ni
  mayúsculas). Una entrada que es una lista vale con **cualquiera** de sus
  alternativas. Se comparan palabras o frases completas; con `*` final casa el
  comienzo de palabra (`"vend*"` casa con «vendió» y «vendido»).
- `metric_value`: la cifra **tal como se dice** en el vídeo («170 millones» →
  170, con la escala en `metric_unit`). Solo cuenta para la exactitud numérica y
  para desempatar; pon `null` si la afirmación no lleva cifra.
- `ts`: solo desempata entre varias candidatas (la más cercana).
- `statement`, `type`, `metric_unit`: informativos, no intervienen en el cálculo.
- `status`: cámbialo a `revisado` cuando hayas comprobado el fichero contra el vídeo.

## Estado

Los dos ficheros iniciales están redactados a mano **a partir de los informes
en prosa, no de las transcripciones**: son un borrador. Revisa cifras, marcas
de tiempo y palabras clave contra el vídeo antes de fiarte de las métricas.
