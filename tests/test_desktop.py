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
            plan = json.loads(output.getvalue().splitlines()[0])["stats"]
            self.assertEqual((plan["to_extract"], plan["calls"]), (2, 2))

            with patch.object(worker, "call_meta", return_value=answer) as call:
                events = self.queue(db, action="extract", engine="meta", key="secret-test", model="muse-spark-1.3")
            self.assertEqual(call.call_count, 2)
            self.assertIn("nunca instrucciones", call.call_args.args[3])  # prompt de extracción, no el del informe
            final = [e for e in events if e["type"] == "queue"][-1]
            self.assertEqual([(j["state"], j["detail"]) for j in final["jobs"]],
                             [("done", "1 afirmaciones verificadas")] * 2)
            self.assertTrue(any("Uno · Tramo 1/1" in e.get("message", "") for e in events))
            self.assertEqual(events[-1]["type"], "done")
            self.assertNotIn("secret-test", json.dumps(events))
            with Store(db) as store:
                self.assertEqual(store.stats()["claims_by_status"], {"verified": 2})
                self.assertNotIn("secret-test", "\n".join(store.db.iterdump()))
                self.assertEqual(store.get_run(1)["backend"], "meta")

    def test_extract_stops_at_once_when_the_key_is_rejected(self):
        import tempfile
        from rezme import Store
        with tempfile.TemporaryDirectory() as tmp:
            db = self.extraction_db(tmp)
            with patch.object(worker, "call_meta", side_effect=worker.MetaAccessError("La clave API no es válida.")) as call:
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
        from rezme import Claim, Store
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
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                worker.run_library({"db": db, "query": "inflacion"})
            library, hits = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(library["stats"], {"videos": 1, "verified": 1, "ungrounded": 1, "entities": 1,
                                            "to_extract": 0, "calls": 0})
        self.assertEqual(library["sources"][0]["detail"], "1.200 frases · subtítulos automáticos")
        self.assertEqual((library["sources"][0]["title"], library["sources"][0]["verified"]), ("Macro 2027", 1))
        self.assertEqual([h["statement"] for h in hits["items"]], ["La inflación subyacente baja al 2,4 %."])
        self.assertEqual(hits["items"][0]["meta"], "statistic · IPC · Macro 2027 · 00:01:15")


if __name__ == "__main__":
    unittest.main()
