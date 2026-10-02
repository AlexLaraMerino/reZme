import contextlib
import io
import json
import unittest
from unittest.mock import patch
from desktop import worker


class DesktopTests(unittest.TestCase):
    def execute(self, **changes):
        options = dict(url="https://www.youtube.com/watch?v=example", mode="prompt", transcript="[00:00:00] Una idea útil.")
        options.update(changes)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            worker.run(options)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_prompt_never_calls_api(self):
        with patch.object(worker, "call_meta", side_effect=AssertionError("API must not run")):
            result = self.execute()[-1]
        self.assertEqual(result["type"], "result")
        self.assertIn("[00:00:00] Una idea útil.", result["text"])
        self.assertIn("Tesis principal", result["text"])

    def test_api_requires_key_before_fetch(self):
        with patch.object(worker.digest, "fetch_subtitles", side_effect=AssertionError("download must not run")):
            with self.assertRaisesRegex(ValueError, "clave"):
                self.execute(mode="api", transcript="")

    def test_api_result_and_secret_not_emitted(self):
        with patch.object(worker, "call_meta", return_value="## Informe\nTexto") as call:
            events = self.execute(mode="api", key="secret-test", model="muse-spark-1.3")
        self.assertEqual(call.call_count, 1)
        self.assertIn("## Informe", events[-1]["text"])
        self.assertNotIn("secret-test", json.dumps(events))

    def test_subtitles_path(self):
        with patch.object(worker.digest, "fetch_subtitles", return_value=({"title":"Prueba", "duration":60}, [(0,"Hola")])):
            result = self.execute(transcript="")[-1]
        self.assertEqual(result["title"], "Prueba")
        self.assertIn("Hola", result["text"])

    def test_missing_subtitles_transcribes(self):
        with patch.object(worker.digest, "fetch_subtitles", return_value=({}, [])), patch.object(worker.digest, "transcribe_audio", return_value=[(0,"Audio")]) as transcribe:
            self.assertIn("Audio", self.execute(transcript="")[-1]["text"])
        transcribe.assert_called_once()

    def test_reject_non_youtube(self):
        for url in ["https://youtube.com.evil.test/watch?v=a", "file:///etc/passwd", "https://example.com"]:
            with self.assertRaises(ValueError):
                worker.validate_url(url)

    def test_long_transcript_is_split(self):
        with patch.object(worker, "call_meta", return_value="Notas") as call:
            self.execute(mode="api", key="test", model="test", transcript=("Una línea de contenido.\n"*10000))
        self.assertGreater(call.call_count, 2)

    def test_api_request_and_truncation(self):
        response = io.StringIO(json.dumps({"choices":[{"finish_reason":"length", "message":{"content":"partial"}}]}))
        with patch.object(worker.urllib.request, "urlopen", return_value=response) as request:
            with self.assertRaisesRegex(RuntimeError, "incompleto"):
                worker.call_meta("prompt", "test", "muse-spark-1.3")
        self.assertEqual(request.call_args.args[0].full_url, "https://api.meta.ai/v1/chat/completions")

    def queue(self, db, **options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            worker.run_queue(dict(mode="queue", db=db, **options))
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_queue_adds_playlist_and_saves_transcripts(self):
        import os, tempfile
        from rezme import Store, batch
        ids = ["aaaaaaaaaa1", "bbbbbbbbbb2", "cccccccccc3"]
        entries = [batch.PlaylistEntry(ids[0], "Uno"), batch.PlaylistEntry(ids[1], "Dos"),
                   batch.PlaylistEntry(ids[2], "Tres"), batch.PlaylistEntry(None, "[Private video]", "vídeo privado")]
        def fetch(url, langs, tmp, cookies):
            video = url.split("v=")[1]
            if video == ids[1]:
                raise RuntimeError("Private video")
            subs = {} if video == ids[2] else {"automatic_captions": {"es": [{"ext": "json3", "url": "u"}]}}
            return dict(title=f"Vídeo {video[0]}", **subs), ([] if video == ids[2] else [(0, "hola")])
        deps = batch.Deps(fetch=fetch, transcribe=lambda *a: self.fail("Whisper desactivado"),
                          sleep=lambda s: None, clock=lambda: 0.0, uniform=lambda a, b: 0.0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(batch, "expand_playlist", return_value=entries), \
                patch.object(batch, "Deps", lambda: deps):
            db = os.path.join(tmp, "data", "rezme.db")
            events = self.queue(db, action="run", browser="chrome",
                                urls="https://www.youtube.com/watch?v=aaaaaaaaaa1&list=PLabcdefghij0123456789")
            self.assertIn("3 vídeos añadidos · 0 ya estaban · 1 no accesibles", [e.get("message") for e in events])
            self.assertTrue(any(j["state"] == "running" for e in events if e["type"] == "queue" for j in e["jobs"]))
            final = [e for e in events if e["type"] == "queue"][-1]
            self.assertEqual([(j["title"], j["state"]) for j in final["jobs"]],
                             [("Vídeo a", "saved"), ("Dos", "failed"), ("Tres", "skipped")])
            self.assertEqual(final["jobs"][1]["detail"], "vídeo privado")
            self.assertEqual(final["summary"], "1 guardados · 0 en cola · 1 fallidos · 1 sin subtítulos")
            self.assertEqual(events[-1]["type"], "done")
            self.assertNotIn("chrome", json.dumps(events))
            with Store(db) as store:
                self.assertEqual(store.stats()["transcripts"], 1)

            retried = self.queue(db, action="retry")[-1]
            self.assertEqual([j["state"] for j in retried["jobs"]], ["saved", "pending", "pending"])
            cleared = self.queue(db, action="clear")[-1]
            self.assertEqual(len(cleared["jobs"]), 3)  # solo se quitan los que ya tienen afirmaciones
            with Store(db) as store:
                self.assertEqual(store.stats()["transcripts"], 1)  # limpiar la cola no borra lo guardado

    def extraction_db(self, tmp):
        import os
        from rezme import Store
        db = os.path.join(tmp, "rezme.db")
        cues = [(0.0, "Yo estimo que la eficiencia espectral realista,"),
                (6.0, "con doble polarización, es de 1,36 bits por segundo y hercio.")]
        with Store(db) as store:
            for video, title in (("aaaaaaaaaa1", "Uno"), ("bbbbbbbbbb2", "Dos")):
                src, _ = store.add_source("youtube", video, title=title, duration_s=600)
                store.save_transcript(src, cues, "subtitles_auto")
                store.enqueue_job(video, f"https://www.youtube.com/watch?v={video}", title=title, stage="extract")
        return db

    def test_extract_runs_from_the_app_with_meta_and_keeps_the_key_private(self):
        import tempfile
        from rezme import Store
        answer = json.dumps({"entities": [], "claims": [{
            "statement": "El autor estima una eficiencia espectral de 1,36 bps/Hz.", "type": "own_calculation",
            "metric_value": 1.36, "quote": "con doble polarización, es de 1,36 bits por segundo y hercio"}]})
        with tempfile.TemporaryDirectory() as tmp:
            db = self.extraction_db(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                worker.run_library({"db": db})
            library = json.loads(output.getvalue().splitlines()[0])
            plan = library["stats"]
            self.assertEqual((plan["to_extract"], plan["calls"], plan["calibrated"]), (2, 2, 0))
            first = library["sources"][0]
            self.assertEqual((first["calls"], first["job"]), (1, 2))
            self.assertGreater(first["tokens_in"], 2000)  # las instrucciones pesan más que el texto
            self.assertGreater(first["tokens_out"], 0)

            with patch.object(worker, "call_meta_extract", return_value=answer) as call:
                events = self.queue(db, action="extract", engine="meta", key="secret-test", model="muse-spark-1.3")
            self.assertEqual(call.call_count, 2)
            self.assertIn("nunca instrucciones", call.call_args.args[0])  # prompt de extracción, no el del informe
            final = [e for e in events if e["type"] == "queue"][-1]
            self.assertEqual([(j["state"], j["detail"]) for j in final["jobs"]],
                             [("done", "1 afirmaciones verificadas")] * 2)
            self.assertTrue(any("Uno · Tramo 1/1" in e.get("message", "") for e in events))
            self.assertEqual(events[-1]["type"], "done")
            self.assertIn("gasto de la tanda", events[-1]["message"])
            # Volver a extraer un vídeo ya hecho lo devuelve a la lista sin borrar lo anterior.
            again = self.queue(db, action="reextract", source="1")[-1]
            self.assertEqual([j["state"] for j in again["jobs"]], ["saved", "done"])
            with self.assertRaisesRegex(ValueError, "transcripción"):
                self.queue(db, action="reextract", source="99")
            self.assertEqual(len([e for e in events if e["type"] == "spend"]), 2)
            self.assertNotIn("secret-test", json.dumps(events))
            with Store(db) as store:
                self.assertEqual(store.stats()["claims_by_status"], {"verified": 2})
                self.assertGreater(store.get_run(1)["stats"]["consumo"]["entrada"], 2000)
                self.assertNotIn("secret-test", "\n".join(store.db.iterdump()))
                self.assertEqual(store.get_run(1)["backend"], "meta")

    def test_extract_respects_selection_budget_and_calibrates_estimates(self):
        import tempfile
        from rezme import Store
        answer = json.dumps({"entities": [], "claims": []})

        def meta(system, user, key, model, usage=None, notify=None, reasoning=None):
            usage.update(prompt_tokens=100_000, completion_tokens=50_000)
            return answer

        options = dict(action="extract", engine="meta", key="k", model="m", price_in="2", price_out="10")
        with tempfile.TemporaryDirectory() as tmp:
            db = self.extraction_db(tmp)
            # Solo el vídeo elegido (trabajo 2), aunque haya dos pendientes.
            with patch.object(worker, "call_meta_extract", side_effect=meta) as call:
                events = self.queue(db, ids="2", budget="5", **options)
            self.assertEqual(call.call_count, 1)
            spend = [e for e in events if e["type"] == "spend"][-1]
            self.assertEqual((spend["cost"], spend["tokens_in"], spend["tokens_out"]), (0.7, 100_000, 50_000))
            with Store(db) as store:
                self.assertEqual([j["status"] for j in store.list_jobs()], ["pending", "done"])
                self.assertAlmostEqual(store.get_run(1)["cost_usd"], 0.7)

            # Con lo medido, la estimación del que queda deja de ser la de por defecto.
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                worker.run_library({"db": db})
            library = json.loads(output.getvalue().splitlines()[0])
            pending = [item for item in library["sources"] if "job" in item][0]
            self.assertEqual(library["stats"]["calibrated"], 1)
            self.assertGreater(pending["tokens_out"], 40_000)

    def test_extract_stops_when_the_budget_is_reached(self):
        import tempfile
        from rezme import Store
        cues = [(i * 30.0, f"frase {i} con algo de contenido") for i in range(80)]  # 40 min: 4-5 tramos

        def meta(system, user, key, model, usage=None, notify=None, reasoning=None):
            usage.update(prompt_tokens=100_000, completion_tokens=0)
            return json.dumps({"entities": [], "claims": []})

        with tempfile.TemporaryDirectory() as tmp:
            db = self.extraction_db(tmp)
            with Store(db) as store:
                store.save_transcript(1, cues, "whisper")
            with patch.object(worker, "call_meta_extract", side_effect=meta) as call:
                events = self.queue(db, action="extract", engine="meta", key="k", model="m",
                                    price_in="2", price_out="10", budget="0.5")
            self.assertEqual(call.call_count, 3)  # 0,20 $ por llamada: la cuarta ya no se hace
            self.assertIn("Tope de gasto alcanzado (0.60 $)", events[-1]["message"])
            with Store(db) as store:
                self.assertEqual({j["status"] for j in store.list_jobs()}, {"pending"})
                done = store.get_run(1)["stats"]["tramos"]
                self.assertEqual(len([t for t in done.values() if t["estado"] == "ok"]), 3)
            # Otra tanda continúa donde se quedó, sin repetir tramos.
            with patch.object(worker, "call_meta_extract", side_effect=meta) as call:
                self.queue(db, action="extract", engine="meta", key="k", model="m",
                           price_in="2", price_out="10", budget="50")
            with Store(db) as store:
                tramos = len(store.get_run(1)["stats"]["tramos"])
                self.assertEqual(call.call_count, tramos - 3 + 1)  # lo que faltaba + el segundo vídeo
                self.assertEqual({j["status"] for j in store.list_jobs()}, {"done"})

    def test_extract_stops_at_once_when_the_key_is_rejected(self):
        import tempfile
        from rezme import Store
        with tempfile.TemporaryDirectory() as tmp:
            db = self.extraction_db(tmp)
            with patch.object(worker, "call_meta_extract", side_effect=worker.MetaAccessError("La clave API no es válida.")) as call:
                with self.assertRaisesRegex(RuntimeError, "Extracción detenida: La clave API no es válida."):
                    self.queue(db, action="extract", engine="meta", key="mala", model="m")
            self.assertEqual(call.call_count, 1)  # ni reintentos ni el segundo vídeo
            with Store(db) as store:
                self.assertEqual({j["status"] for j in store.list_jobs()}, {"pending"})
            with self.assertRaisesRegex(ValueError, "clave de Meta"):
                self.queue(db, action="extract", engine="meta", key="")
            with patch("rezme.backends.check_backend", return_value="No encuentro el CLI"):
                with self.assertRaisesRegex(ValueError, "Claude Code"):
                    self.queue(db, action="extract", engine="claude-code")

    def meta_responses(self, *items):
        """Simula la API de Meta: cada elemento es una respuesta (dict) o un error HTTP (código, cabeceras, cuerpo)."""
        import urllib.error
        queue = list(items)
        sent = []

        def urlopen(request, timeout=None):
            sent.append(json.loads(request.data))
            item = queue.pop(0)
            if isinstance(item, tuple):
                code, headers, body = item
                raise urllib.error.HTTPError(request.full_url, code, "error", headers, io.BytesIO(body.encode()))
            return io.StringIO(json.dumps(item))
        return urlopen, sent

    def test_extraction_waits_and_retries_when_meta_rate_limits(self):
        ok = {"choices": [{"finish_reason": "stop", "message": {"content": '{"claims": []}'}}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        urlopen, sent = self.meta_responses((429, {"Retry-After": "7"}, "rate limit exceeded"),
                                            (429, {}, "too many requests"), (503, {}, ""), ok, ok, ok)
        notes, usage = [], {}
        worker._pace["seconds"] = 0.0
        with patch.object(worker.urllib.request, "urlopen", urlopen), patch("time.sleep") as sleep:
            text = worker.call_meta_extract("SISTEMA", "texto", "k", "m", usage, notes.append)
            self.assertEqual(text, '{"claims": []}')
            # Respeta lo que pide Meta, después su propia espera creciente, y el error 503 aparte.
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [7.0, 40, 10])
            self.assertIn("espero 7 s y sigo (intento 1 de 5)", notes[0])
            self.assertEqual(usage["completion_tokens"], 5)
            self.assertEqual(sent[0]["max_completion_tokens"], worker.EXTRACT_MAX_TOKENS)
            self.assertEqual(sent[0]["messages"][0], {"role": "system", "content": "SISTEMA"})
            # Tras los 429 deja una pausa entre llamadas, que se va relajando.
            self.assertEqual(worker._pace["seconds"], 8.0)
            sleep.reset_mock()
            worker.call_meta_extract("S", "u", "k", "m")
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [8.0])
            self.assertEqual(worker._pace["seconds"], 6.4)
        worker._pace["seconds"] = 0.0

    def test_extraction_gives_up_cleanly_if_meta_keeps_limiting(self):
        import tempfile
        from rezme import Store
        urlopen, sent = self.meta_responses(*[(429, {}, "too many requests")] * 6)
        worker._pace["seconds"] = 0.0
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker.urllib.request, "urlopen", urlopen), \
                patch("time.sleep") as sleep:
            db = self.extraction_db(tmp)
            with self.assertRaisesRegex(RuntimeError, "sigue limitando las peticiones"):
                self.queue(db, action="extract", engine="meta", key="k", model="m", price_in="1", price_out="1")
            self.assertEqual((len(sent), sleep.call_count), (6, 5))  # un solo vídeo, sin reintentos en cadena
            with Store(db) as store:
                self.assertEqual({j["status"] for j in store.list_jobs()}, {"pending"})
        worker._pace["seconds"] = 0.0

    def test_slow_model_is_given_time_and_reports_it_is_alive(self):
        import time as real_time
        ok = {"choices": [{"finish_reason": "stop", "message": {"content": '{"claims": []}'}}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 900,
                        "completion_tokens_details": {"reasoning_tokens": 700}}}
        seen = {}

        def slow(request, timeout=None):
            seen["timeout"] = timeout
            real_time.sleep(0.08)
            return io.StringIO(json.dumps(ok))

        notes, usage = [], {}
        worker._pace["seconds"] = 0.0
        with patch.object(worker.urllib.request, "urlopen", slow), patch.object(worker, "HEARTBEAT_S", 0.02):
            worker.call_meta_extract("S", "u", "k", "m", usage, notes.append)
        self.assertEqual(seen["timeout"], worker.EXTRACT_TIMEOUT)
        self.assertGreaterEqual(worker.EXTRACT_TIMEOUT, 600)
        self.assertTrue(notes and all("esperando la respuesta del modelo" in n for n in notes))
        self.assertEqual(usage["completion_tokens_details"]["reasoning_tokens"], 700)

    def test_timeout_is_retried_only_once(self):
        calls = []

        def timed_out(request, timeout=None):
            calls.append(timeout)
            raise TimeoutError("The read operation timed out")

        notes = []
        worker._pace["seconds"] = 0.0
        with patch.object(worker.urllib.request, "urlopen", timed_out), patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                worker.call_meta_extract("S", "u", "k", "m", None, notes.append)
        self.assertEqual((len(calls), sleep.call_count), (2, 1))
        self.assertIn("The read operation timed out", notes[-1])

    def test_low_reasoning_is_requested_and_dropped_if_the_model_rejects_it(self):
        ok = {"choices": [{"finish_reason": "stop", "message": {"content": '{"claims": []}'}}]}
        urlopen, sent = self.meta_responses(ok, (400, {}, "Unsupported parameter: reasoning_effort"), ok, ok)
        notes = []
        worker._pace["seconds"] = 0.0
        worker._reasoning["supported"] = True
        try:
            with patch.object(worker.urllib.request, "urlopen", urlopen), patch("time.sleep"):
                worker.call_meta_extract("S", "u", "k", "m", None, notes.append, reasoning="low")
                self.assertEqual(sent[0]["reasoning_effort"], "low")
                # El modelo lo rechaza: se reintenta sin el ajuste y ya no se vuelve a enviar.
                worker.call_meta_extract("S", "u", "k", "m", None, notes.append, reasoning="low")
                self.assertEqual(("reasoning_effort" in sent[1], "reasoning_effort" in sent[2]), (True, False))
                self.assertEqual(sent[2]["max_completion_tokens"], worker.EXTRACT_MAX_TOKENS)
                self.assertIn("no admite ajustar el razonamiento", notes[0])
                worker.call_meta_extract("S", "u", "k", "m", None, notes.append, reasoning="low")
                self.assertNotIn("reasoning_effort", sent[3])
        finally:
            worker._reasoning["supported"] = True
        # Sin ajuste (el del modelo), no se envía nada.
        urlopen, sent = self.meta_responses(ok)
        with patch.object(worker.urllib.request, "urlopen", urlopen):
            worker.call_meta_extract("S", "u", "k", "m")
        self.assertNotIn("reasoning_effort", sent[0])

    def test_app_extraction_passes_speed_settings_and_counts_usage_across_threads(self):
        import tempfile
        from rezme import Store
        cues = [(i * 20.0, f"frase número {i} del vídeo de prueba") for i in range(60)]  # 4 tramos
        seen = []

        def meta(system, user, key, model, usage=None, notify=None, reasoning=None):
            seen.append(reasoning)
            usage.update(prompt_tokens=1000, completion_tokens=500,
                         completion_tokens_details={"reasoning_tokens": 400})
            return json.dumps({"claims": []})

        with tempfile.TemporaryDirectory() as tmp:
            db = self.extraction_db(tmp)
            with Store(db) as store:
                store.save_transcript(1, cues, "whisper")
            with patch.object(worker, "call_meta_extract", side_effect=meta):
                events = self.queue(db, action="extract", engine="meta", key="k", model="m", ids="1",
                                    price_in="1", price_out="2", workers="3", reasoning="low")
            self.assertEqual(seen, ["low"] * 4)
            self.assertTrue(any("4 tramos, 3 a la vez" in e.get("message", "") for e in events))
            spend = [e for e in events if e["type"] == "spend"][-1]
            self.assertEqual((spend["tokens_in"], spend["tokens_out"], spend["cost"]), (4000, 2000, 0.008))
            with Store(db) as store:
                usage = store.get_run(1)["stats"]["consumo"]
                self.assertEqual((usage["entrada"], usage["salida"], usage["razonamiento"], usage["llamadas"]),
                                 (4000, 2000, 1600, 4))

    def test_exhausted_quota_is_not_retried(self):
        urlopen, sent = self.meta_responses((429, {}, '{"error": {"message": "Insufficient balance"}}'))
        with patch.object(worker.urllib.request, "urlopen", urlopen), patch("time.sleep") as sleep:
            with self.assertRaisesRegex(worker.MetaAccessError, "saldo o la cuota"):
                worker.call_meta_extract("S", "u", "k", "m")
        self.assertEqual((len(sent), sleep.call_count), (1, 0))

    def test_truncated_meta_answer_is_salvaged_instead_of_failing(self):
        import tempfile
        from rezme import Store
        cut = ('{"entities": [], "claims": [{"statement": "El autor estima 1,36 bps/Hz.", "type": "own_calculation", '
               '"metric_value": 1.36, "quote": "con doble polarización, es de 1,36 bits por segundo y hercio"}, '
               '{"statement": "Esta se queda a med')
        answer = {"choices": [{"finish_reason": "length", "message": {"content": cut}}]}
        urlopen, sent = self.meta_responses((400, {}, "max_completion_tokens is too large"), answer, answer)
        worker._pace["seconds"] = 0.0
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker.urllib.request, "urlopen", urlopen), \
                patch("time.sleep"):
            db = self.extraction_db(tmp)
            events = self.queue(db, action="extract", engine="meta", key="k", model="m", price_in="1", price_out="1")
            # Si el modelo no admite respuestas tan largas, se baja al límite anterior.
            self.assertEqual([p["max_completion_tokens"] for p in sent],
                             [worker.EXTRACT_MAX_TOKENS, worker.REPORT_MAX_TOKENS, worker.EXTRACT_MAX_TOKENS])
            self.assertEqual(events[-1]["type"], "done")
            with Store(db) as store:
                self.assertEqual(store.stats()["claims_by_status"], {"verified": 2})
                self.assertTrue(store.get_run(1)["stats"]["tramos"]["0"]["truncada"])

    def catalog(self, db, **options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            worker.run_catalog(dict(mode="catalog", db=db, **options))
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_catalog_cleaning_from_the_app(self):
        import os, tempfile
        from rezme import Claim, Store
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "rezme.db")
            with Store(db) as store:
                src, _ = store.add_source("youtube", "aaaaaaaaaa1", title="V")
                ids = {}
                for type_, name, n in (("central_bank", "Reserva Federal", 2), ("central_bank", "Federal Reserve", 3),
                                       ("technology", "virtual cell", 2), ("technology", "virtual cells", 1),
                                       ("company", "Amazon", 1)):
                    ids[name] = store.upsert_entity(type_, name)
                    for i in range(n):
                        store.add_claim(Claim(source_id=src, statement=f"{name} {i}", type="fact", status="verified",
                                              entity_id=ids[name], quote=f"cita número {i} sobre {name} en el vídeo"))
                store.add_claim(Claim(source_id=src, statement="Repetida.", type="fact", status="verified",
                                      quote="cita número 0 sobre Amazon en el vídeo"))
            empty = self.catalog(db, action="status")[-1]
            self.assertEqual((empty["type"], empty["proposals"], empty["entities"], empty["duplicates"]), ("catalog", [], 5, 1))

            answer = json.dumps({"groups": [{"ids": [ids["Reserva Federal"], ids["Federal Reserve"]], "reason": "mismo banco"}]})
            with patch.object(worker, "call_meta_extract", return_value=answer) as call:
                events = self.catalog(db, action="propose", use_model="1", engine="meta", key="secret-test", model="m",
                                      price_in="1", price_out="1", budget="5")
            self.assertTrue(call.called)
            self.assertIn("exactamente la misma cosa", call.call_args.args[0])
            self.assertNotIn("secret-test", json.dumps(events))
            listing = [e for e in events if e["type"] == "catalog"][-1]["proposals"]
            got = {(p["drop"]["name"], p["keep"]["name"], p["origin"], p["keep"]["type"]) for p in listing}
            self.assertEqual(got, {("virtual cells", "virtual cell", "rule", "tecnología"),
                                   ("Reserva Federal", "Federal Reserve", "model", "banco central")})
            self.assertIn("2 propuestas nuevas (1 por coincidencia de nombre, 1 sugeridas por el modelo", events[-1]["message"])

            rule = next(p["id"] for p in listing if p["origin"] == "rule")
            model = next(p["id"] for p in listing if p["origin"] == "model")
            after = self.catalog(db, action="dismiss", ids=str(rule))[-1]
            self.assertEqual([p["id"] for p in after["proposals"]], [model])
            with self.assertRaisesRegex(ValueError, "seleccionada"):
                self.catalog(db, action="apply", ids="")
            events = self.catalog(db, action="apply", ids=str(model))
            self.assertIn("1 entidades fusionadas · 2 afirmaciones reasignadas", events[-1]["message"])
            self.assertEqual((events[-2]["entities"], events[-2]["merged"], events[-2]["proposals"]), (4, 1, []))
            merge = events[-2]["merges"][0]
            self.assertEqual((merge["drop"], merge["keep"], merge["claims"], merge["undoable"]),
                             ("Reserva Federal", "Federal Reserve", 2, True))
            undone = self.catalog(db, action="undo", ids=str(merge["id"]))
            self.assertIn("Fusión deshecha: 2 afirmaciones", undone[-1]["message"])
            self.assertEqual((undone[-2]["entities"], undone[-2]["merges"], undone[-2]["proposals"]), (5, [], []))
            with Store(db) as store:
                store.merge_entities(ids["Federal Reserve"], store.resolve_entity("Reserva Federal"))
            self.assertTrue(os.path.exists(db + ".antes-de-fusionar.bak"))
            events = self.catalog(db, action="dedupe")
            self.assertIn("1 afirmaciones repetidas retiradas", events[-1]["message"])
            self.assertEqual(events[-2]["duplicates"], 0)
            with self.assertRaises(ValueError):
                self.catalog(db, action="borrar-todo")

    def test_contrast_from_the_app_and_point_in_time_search(self):
        import os, tempfile
        from rezme import Claim, Store
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "rezme.db")
            with Store(db) as store:
                leo, _ = store.add_source("youtube", "aaaaaaaaaa1", title="Leo sobre AST", channel="Leo",
                                          channel_id="LEO", published_at="2026-03-01")
                eme, _ = store.add_source("youtube", "bbbbbbbbbb2", title="Emérito sobre AST", channel="Emérito",
                                          channel_id="EME", published_at="2026-09-01")
                asts = store.upsert_entity("company", "AST SpaceMobile")
                runs = {src: store.start_run(source_id=src, prompt_version="v5") for src in (leo, eme)}
                yes, _ = store.add_claim(Claim(source_id=leo, statement="AST dará banda ancha masiva.", type="fact",
                                               status="verified", entity_id=asts, run_id=runs[leo]))
                no, _ = store.add_claim(Claim(source_id=eme, statement="AST no dará banda ancha: solo respaldo.",
                                              type="fact", status="verified", entity_id=asts, run_id=runs[eme]))
            run = lambda **options: self._run(worker.run_cross, dict(mode="cross", db=db, **options))
            before = run(action="status")[-1]
            self.assertEqual((before["stats"]["entidades"], before["stats"]["pendientes"], before["stats"]["llamadas"],
                              before["pairs"]), (1, 1, 1, []))
            answer = json.dumps({"relations": [{"a": no, "b": yes, "relation": "contradicts", "reason": "capacidad"}]})
            with patch.object(worker, "call_meta_extract", return_value=answer) as call:
                events = run(action="run", engine="meta", key="secret-test", model="m", price_in="1", price_out="1",
                             budget="5", workers="2")
            self.assertIn("canales distintos", call.call_args.args[0])
            self.assertNotIn("secret-test", json.dumps(events))
            self.assertIn("1 entidades contrastadas · 0 coincidencias, 1 contradicciones", events[-1]["message"])
            pair = [e for e in events if e["type"] == "cross"][-1]["pairs"][0]
            self.assertEqual((pair["relation"], pair["entity"], pair["a"]["channel"], pair["b"]["channel"], pair["reason"]),
                             ("contradicts", "AST SpaceMobile", "Emérito", "Leo", "capacidad"))
            with patch.object(worker, "call_meta_extract", side_effect=worker.MetaAccessError("La clave API no es válida.")):
                with Store(db) as store:
                    store.add_claim(Claim(source_id=eme, statement="Otra más.", type="fact", status="verified", entity_id=asts))
                with self.assertRaisesRegex(RuntimeError, "Contraste detenido: La clave API no es válida."):
                    run(action="run", engine="meta", key="k", model="m")

            # La ficha del vídeo y el buscador muestran el contraste.
            library = lambda **options: self._run(worker.run_library, dict(db=db, **options))
            detail = next(e for e in library(source=str(leo)) if e["type"] == "detail")["claims"][0]
            self.assertEqual((detail["supported_by"], detail["contradicted_by"]), (0, 1))
            self.assertEqual(detail["cross"], [{"kind": "contradicts", "text":
                             "Lo contradice Emérito: AST no dará banda ancha: solo respaldo. (capacidad)"}])
            self.assertEqual(detail["relations"], [])
            hits = lambda **options: next(e for e in library(query="banda ancha", **options) if e["type"] == "hits")["items"]
            self.assertEqual({h["statement"]: h["contradicted_by"] for h in hits()},
                             {"AST dará banda ancha masiva.": 1, "AST no dará banda ancha: solo respaldo.": 1})
            # «Qué se sabía el 1 de junio»: el vídeo de septiembre aún no existía.
            self.assertEqual([h["statement"] for h in hits(known_at="2026-06-01")], ["AST dará banda ancha masiva."])
            self.assertEqual(hits(known_at="2025-01-01"), [])
            with self.assertRaisesRegex(ValueError, "AAAA-MM-DD"):
                hits(known_at="ayer")

    def _run(self, function, options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function(options)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_queue_status_empty_and_bad_input(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "rezme.db")
            empty = self.queue(db, action="status")[0]
            self.assertEqual((empty["jobs"], empty["summary"], empty["counts"]["pending"]), ([], "", 0))
            with self.assertRaisesRegex(ValueError, "URL válida"):
                self.queue(db, action="run", urls="https://example.com/x")
            with self.assertRaises(ValueError):
                self.queue(db, action="borrar")
        with self.assertRaises(ValueError):
            self.queue("", action="status")

    def test_library_lists_saved_videos_and_searches_verified_claims(self):
        import os, tempfile
        from rezme import Claim, Implication, Store
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "rezme.db")
            with Store(db) as store:
                src, _ = store.add_source("youtube", "aaaaaaaaaa1", title="Macro 2027", channel="Canal",
                                          published_at="2026-09-01")
                store.save_transcript(src, [(float(i), "x") for i in range(1200)], "subtitles_auto")
                store.add_source("youtube", "bbbbbbbbbb2", title="Sin transcripción")
                entity = store.upsert_entity("macro_indicator", "IPC")
                store.add_claim(Claim(source_id=src, statement="La inflación subyacente baja al 2,4 %.",
                                      type="statistic", status="verified", entity_id=entity, ts_start=75))
                store.add_claim(Claim(source_id=src, statement="La inflación se dispara.", type="opinion",
                                      status="ungrounded"))
                run = store.start_run(source_id=src, prompt_version="v1")
                good, _ = store.add_claim(Claim(
                    source_id=src, run_id=run, statement="El petróleo supera los 100 dólares.", type="risk",
                    status="verified", entity_id=entity, metric_value=100.0, metric_unit="USD", ts_start=235.4,
                    quote="el petróleo por encima de $100", title="Petróleo caro y contagio",
                    mechanism=[{"text": "La energía encarece el resto de precios.", "basis": "stated_by_source",
                                "quote": "se contagia"}],
                    fails_when=[{"text": "El petróleo baja pronto.", "basis": "inferred_by_system"}],
                    tags=["macro", "risk"]))
                store.add_implication(Implication(claim_id=good, direction="negative", basis="inferred_by_system",
                                                  target_label="bonos largos", mechanism="más inflación"))
                store.add_claim(Claim(source_id=src, run_id=run, statement="Bajó una décima.", type="statistic",
                                      status="ungrounded", metric_value=0.1,
                                      attrs={"grounding": {"ok": False, "motivos": ["la cifra 0.1 no aparece en el tramo"]}}))
                store.add_claim(Claim(source_id=src, run_id=run, statement="Versión antigua.", type="fact",
                                      status="superseded"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                worker.run_library({"db": db, "query": "inflacion", "source": str(src)})
            library, detail, hits = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual((detail["type"], detail["title"], len(detail["claims"])), ("detail", "Macro 2027", 2))
        claim = detail["claims"][0]
        self.assertEqual((claim["verified"], claim["kind"], claim["entity"], claim["metric"], claim["time"]),
                         (True, "Riesgo", "IPC", "100 USD", "00:03:55"))
        self.assertEqual(claim["link"], "https://www.youtube.com/watch?v=aaaaaaaaaa1&t=235s")
        self.assertEqual(claim["implications"], ["bonos largos: negativo — más inflación (deducido por el modelo)"])
        self.assertEqual((claim["title"], claim["tags"]), ("Petróleo caro y contagio", ["macro", "riesgo"]))
        self.assertEqual(claim["mechanism"], [{"text": "La energía encarece el resto de precios.", "stated": True}])
        self.assertEqual((claim["applies_when"], claim["fails_when"]),
                         ([], [{"text": "El petróleo baja pronto.", "stated": False}]))
        self.assertEqual((detail["claims"][1]["verified"], detail["claims"][1]["reasons"]),
                         (False, ["la cifra 0.1 no aparece en el tramo"]))
        self.assertEqual(hits["items"][0]["statement"], "La inflación subyacente baja al 2,4 %.")
        self.assertEqual(library["stats"], {"videos": 1, "verified": 2, "ungrounded": 2, "entities": 1,
                                            "to_extract": 0, "calls": 0, "calibrated": 0})
        self.assertNotIn("job", library["sources"][0])
        self.assertEqual(library["sources"][0]["detail"], "1.200 frases · subtítulos automáticos")
        self.assertEqual((library["sources"][0]["title"], library["sources"][0]["verified"]), ("Macro 2027", 2))
        self.assertEqual([h["statement"] for h in hits["items"]], ["La inflación subyacente baja al 2,4 %."])
        self.assertEqual(hits["items"][0]["meta"], "statistic · IPC · Macro 2027 · 00:01:15")


if __name__ == "__main__":
    unittest.main()
