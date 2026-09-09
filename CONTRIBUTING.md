# Colaborar con reZme

Esta es una herramienta personal compartida como código abierto. Preferimos cambios pequeños y centrados en problemas reproducibles.

## Comunicar un fallo

Abre un issue indicando tu versión de macOS, si usas Apple Silicon o Intel, el modo elegido, los pasos y el mensaje de error. Incluye una URL pública solo si es necesaria para reproducirlo. Elimina claves API, cookies, datos personales y contenido privado de las capturas.

## Proponer cambios

1. Crea un fork y una rama.
2. Instala con `bash Instalar.command` y realiza el cambio.
3. Ejecuta `.venv/bin/python -m unittest discover -s tests -v`.
4. Si cambias la interfaz, compila con `.venv/bin/python desktop/build.py` y comprueba la app.
5. Abre una pull request explicando el problema, el resultado y lo que has probado.

No añadas dependencias, llamadas externas, telemetría ni almacenamiento de credenciales sin explicar su necesidad. Las pruebas no deben necesitar una clave real ni consumir saldo. No incluyas archivos generados, entornos Python, vídeos ni transcripciones personales.

Las contribuciones se ofrecen bajo la licencia MIT del proyecto. Las dependencias conservan sus propias licencias.
