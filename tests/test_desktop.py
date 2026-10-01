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
            self.assertEqual(len(cleared["jobs"]), 2)
            with Store(db) as store:
                self.assertEqual(store.stats()["transcripts"], 1)  # limpiar la cola no borra lo guardado

    def test_queue_status_empty_and_bad_input(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "rezme.db")
            self.assertEqual(self.queue(db, action="status"), [{"type": "queue", "jobs": [], "summary": ""}])
            with self.assertRaisesRegex(ValueError, "URL válida"):
                self.queue(db, action="run", urls="https://example.com/x")
            with self.assertRaises(ValueError):
                self.queue(db, action="borrar")
        with self.assertRaises(ValueError):
            self.queue("", action="status")


if __name__ == "__main__":
    unittest.main()
