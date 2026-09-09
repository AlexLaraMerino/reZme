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


if __name__ == "__main__":
    unittest.main()
