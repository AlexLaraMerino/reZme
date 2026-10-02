import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rezme import SCHEMA_VERSION, Claim, Implication, Store, batch, cli
from rezme import ingest as ing
from rezme.schema import ValidationError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
IDS = ["aaaaaaaaaa1", "bbbbbbbbbb2", "cccccccccc3", "dddddddddd4", "eeeeeeeeee5"]
PLAYLIST = "https://www.youtube.com/playlist?list=PLabcdefghij0123456789"


def url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def info(title, subs=True):
    data = {"title": title, "channel": "Canal", "upload_date": "20260901", "duration": 600}
    if subs:
        data["automatic_captions"] = {"es": [{"ext": "json3", "url": "u"}]}
    return data


class World:
    """Red, Whisper, reloj y espera simulados."""

    def __init__(self):
        self.videos = {}       # video_id -> (info, cues) o lista de respuestas/excepciones
        self.fetches = []
        self.transcribed = []
        self.sleeps = []
        self.now = 0.0
        self.extracted = []

    def fetch(self, video_url, langs, tmp, cookies):
        video_id = video_url.split("v=")[1]
        self.fetches.append(video_id)
        item = self.videos[video_id]
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, BaseException):
            raise item
        return dict(item[0]), list(item[1])

    def transcribe(self, video_url, tmp, model, lang, cookies):
        self.transcribed.append(video_url.split("v=")[1])
        return [(0.0, "texto de whisper")]

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def extractor(self, store, source_id):
        self.extracted.append(source_id)
        return SimpleNamespace(chunks_failed=0, claims_new=12, verified=10)

    def deps(self, extractor=False):
        return batch.Deps(fetch=self.fetch, transcribe=self.transcribe, sleep=self.sleep,
                          clock=lambda: self.now, uniform=lambda low, high: low,
                          extractor=self.extractor if extractor else None)


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.world = World()
        self.lines = []

    def tearDown(self):
        self.store.close()

    def add(self, *video_ids, **kwargs):
        return batch.add_urls(self.store, [url(v) for v in video_ids], **kwargs)

    def ok(self, video_id, title=None, n=3):
        self.world.videos[video_id] = (info(title or f"Vídeo {video_id[0]}"),
                                       [(float(i), f"cue {i}") for i in range(n)])

    def run_queue(self, extractor=False, **kwargs):
        return batch.run_queue(self.store, deps=self.world.deps(extractor), out=self.lines.append,
                               **kwargs)

    def statuses(self):
        return {j["video_id"]: (j["status"], j["stage"]) for j in self.store.list_jobs()}


class MigrationTests(unittest.TestCase):
    def test_v1_database_migrates_without_losing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "k.db")
            db = sqlite3.connect(path)
            db.executescript((FIXTURES / "schema_v1.sql").read_text(encoding="utf-8"))
            db.executescript("""
                INSERT INTO sources (id, platform, external_id, title, captured_at)
                    VALUES (1, 'youtube', 'fYoi6OjmIlw', 'Hablemos de $ASTS', '2026-09-30');
                INSERT INTO transcripts (source_id, origin, cues_json, n_cues, sha256, created_at)
                    VALUES (1, 'subtitles_auto', '[[0.0,"hola"]]', 1, 'abc', '2026-09-30');
                INSERT INTO extraction_runs (id, model, backend, prompt_version, schema_version,
                    created_at) VALUES (1, 'm', 'api', 'v0', 1, '2026-09-30');
                INSERT INTO claims (source_id, run_id, type, statement, evidence_grade, stance,
                    status, captured_at, fingerprint) VALUES (1, 1, 'fact',
                    'La eficiencia espectral es baja', 'none', 'n/a', 'verified', '2026-09-30', 'f1');
            """)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            db.close()

            with Store(path) as store:
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0],
                                 SCHEMA_VERSION)
                self.assertEqual(SCHEMA_VERSION, 8)
                stats = store.stats()
                self.assertEqual((stats["sources"], stats["transcripts"], stats["claims"],
                                  stats["extraction_runs"], stats["jobs"]), (1, 1, 1, 1, 0))
                self.assertEqual(store.latest_transcript(1)["cues"], [(0.0, "hola")])
                hit = store.search_claims("espectral")[0]  # el índice de búsqueda se reconstruye
                self.assertEqual((hit["statement"], hit["mechanism"], hit["tags"], hit["title"]),
                                 ("La eficiencia espectral es baja", [], [], None))
                self.assertTrue(os.path.exists(path + ".v3.bak"))  # copia de seguridad previa
                # La tabla reconstruida admite los tipos nuevos y sigue enlazada con el resto.
                new_id, _ = store.add_claim(Claim(source_id=1, statement="El apalancamiento operativo amplifica.",
                                                  type="mechanism", status="verified", title="Apalancamiento"))
                store.add_implication(Implication(claim_id=new_id, direction="mixed", basis="inferred_by_system",
                                                  target_label="márgenes", conditional_on="capacidad ociosa"))
                self.assertTrue(store.add_relation(new_id, "related_to", hit["id"]))
                self.assertEqual(len(store.search_claims("apalancamiento")), 2 - 1)
                self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(store.get_run(1)["stats"], {})
                job_id, created = store.enqueue_job("aaaaaaaaaa1", url("aaaaaaaaaa1"))
                self.assertTrue(created)
            with Store(path) as store:  # reabrir no vuelve a migrar ni pierde la cola
                self.assertEqual(store.get_job(job_id)["status"], "pending")

    def test_newer_database_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "k.db")
            db = sqlite3.connect(path)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            db.close()
            with self.assertRaisesRegex(RuntimeError, "más nuevo"):
                Store(path)

    def test_job_constraints(self):
        with Store(":memory:") as store:
            job_id, _ = store.enqueue_job("aaaaaaaaaa1", url("aaaaaaaaaa1"))
            for bad in (dict(status="ok"), dict(stage="publicar"), dict(cookies="x")):
                with self.assertRaises(ValidationError, msg=str(bad)):
                    store.update_job(job_id, **bad)
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute("INSERT INTO jobs (video_id, url, created_at) VALUES (?,?,?)",
                                 ("aaaaaaaaaa1", "u", "now"))


class AddTests(QueueBase):
    def test_dedupes_within_queue(self):
        report = batch.add_urls(self.store, [
            url(IDS[0]), f"https://youtu.be/{IDS[0]}?t=5", url(IDS[1]) + "&t=30s",
            "https://example.com/watch?v=aaaaaaaaaa1", "no es una url"])
        self.assertEqual((report.added, report.already), (2, 1))
        self.assertEqual(len(report.invalid), 2)
        self.assertEqual(self.add(IDS[0]).already, 1)
        jobs = self.store.list_jobs()
        self.assertEqual([j["video_id"] for j in jobs], IDS[:2])
        self.assertEqual(jobs[1]["url"], url(IDS[1]))  # URL canónica, sin parámetros

    def test_dedupes_against_ingested_videos(self):
        source, _ = self.store.add_source("youtube", IDS[0], title="Ya ingerido")
        self.store.save_transcript(source, [(0.0, "hola")], "subtitles_auto")
        self.store.add_source("youtube", IDS[1])  # fuente sin transcripción: sí hay que ingerir
        report = self.add(IDS[0], IDS[1])
        self.assertEqual((report.added, report.already, report.queued_for_extract), (1, 1, 1))
        self.assertEqual(self.statuses(), {IDS[0]: ("pending", "extract"),
                                           IDS[1]: ("pending", "ingest")})
        # Con la extracción completa ya no se encola nada.
        self.store.remove_job(self.store.list_jobs()[0]["id"])
        run = self.store.start_run(source_id=source, prompt_version="v1")
        self.store.update_run(run, stats={"tramos": {"0": {"estado": "ok"}}})
        report = self.add(IDS[0])
        self.assertEqual((report.added, report.already, report.queued_for_extract), (0, 1, 0))
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_playlist_expansion_keeps_order_and_reports_inaccessible(self):
        calls = []

        def expand(playlist_url, cookies_from):
            calls.append((playlist_url, cookies_from))
            return [batch.PlaylistEntry(IDS[0], "Uno"),
                    batch.PlaylistEntry(IDS[1], "[Private video]", "vídeo privado"),
                    batch.PlaylistEntry(IDS[2], "Tres"),
                    batch.PlaylistEntry(None, None, "vídeo no disponible"),
                    batch.PlaylistEntry(IDS[3], "De pago", "vídeo de pago"),
                    batch.PlaylistEntry(IDS[0], "Uno repetido"),
                    batch.PlaylistEntry(IDS[4], "Cinco")]

        self.add(IDS[2])
        report = batch.add_urls(self.store, [PLAYLIST + "&si=TOKENSECRETO"], expand=expand,
                                cookies_from="chrome")
        self.assertEqual(calls, [(PLAYLIST, "chrome")])
        self.assertEqual((report.added, report.already), (2, 2))
        self.assertEqual(report.inaccessible, [("[Private video]", "vídeo privado"),
                                               ("vídeo sin identificar", "vídeo no disponible"),
                                               ("De pago", "vídeo de pago")])
        jobs = self.store.list_jobs()
        self.assertEqual([j["video_id"] for j in jobs], [IDS[2], IDS[0], IDS[4]])
        self.assertEqual((jobs[1]["playlist_url"], jobs[1]["title"]), (PLAYLIST, "Uno"))
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn("TOKENSECRETO", dump)
        self.assertNotIn("chrome", dump)

    def test_video_url_with_list_only_expands_when_asked(self):
        mixed = url(IDS[0]) + "&list=PLabcdefghij0123456789"
        expand = mock.Mock(return_value=[batch.PlaylistEntry(IDS[1], "Otro")])
        self.assertEqual(batch.add_urls(self.store, [mixed], expand=expand).added, 1)
        expand.assert_not_called()
        self.assertEqual(batch.add_urls(self.store, [mixed], expand=expand,
                                        whole_playlist=True).added, 1)
        self.assertEqual([j["video_id"] for j in self.store.list_jobs()], IDS[:2])

    def test_expand_playlist_maps_flat_entries(self):
        flat = {"entries": [
            {"id": IDS[0], "title": "Uno"}, None,
            {"id": IDS[1], "title": "[Deleted video]"},
            {"id": IDS[2], "title": "Miembros", "availability": "subscriber_only"}]}
        seen = {}

        class FakeYDL:
            def __init__(self, opts):
                seen.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, playlist_url, download):
                seen["download"] = download
                return flat

        with mock.patch.dict("sys.modules", {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYDL)}):
            entries = batch.expand_playlist(PLAYLIST, "firefox")
        self.assertEqual([(e.video_id, e.reason) for e in entries], [
            (IDS[0], None), (None, "vídeo no disponible"), (IDS[1], "vídeo eliminado"),
            (IDS[2], "solo para miembros del canal")])
        self.assertEqual((seen["extract_flat"], seen["skip_download"], seen["download"]),
                         (True, True, False))
        self.assertNotIn("playlistend", seen)
        with mock.patch.dict("sys.modules", {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYDL)}):
            batch.expand_playlist("https://www.youtube.com/@LeoCui/videos", None, limit=25)
        self.assertEqual(seen["playlistend"], 25)
        self.assertEqual(seen["cookiesfrombrowser"], ("firefox",))

    def test_channel_urls_are_recognised_and_expanded_with_a_limit(self):
        ok = {"https://www.youtube.com/@LeoCui": "https://www.youtube.com/@LeoCui/videos",
              "https://www.youtube.com/@LeoCui/videos": "https://www.youtube.com/@LeoCui/videos",
              "https://youtube.com/channel/UCJ5gGCjn6ItdPQF0ZO9YROA/featured":
                  "https://www.youtube.com/channel/UCJ5gGCjn6ItdPQF0ZO9YROA/videos",
              "https://www.youtube.com/c/Nombre/": "https://www.youtube.com/c/Nombre/videos"}
        for raw, expected in ok.items():
            self.assertEqual(batch.channel_url(raw), expected, raw)
        for bad in (url(IDS[0]), PLAYLIST, "https://www.youtube.com/", "https://example.com/@LeoCui",
                    "https://www.youtube.com/@LeoCui/about", "https://www.youtube.com/watch"):
            self.assertIsNone(batch.channel_url(bad), bad)

        calls = []

        def expand(target, cookies_from, limit=None):
            calls.append((target, limit))
            return [batch.PlaylistEntry(IDS[0], "Reciente"), batch.PlaylistEntry(IDS[1], "Anterior")][:limit]

        report = batch.add_urls(self.store, ["https://www.youtube.com/@LeoCui", PLAYLIST], expand=expand,
                                channel_limit=1)
        self.assertEqual(calls, [("https://www.youtube.com/@LeoCui/videos", 1), (PLAYLIST, None)])
        self.assertEqual((report.added, report.already, report.channels), (2, 1, 1))
        jobs = self.store.list_jobs()
        self.assertEqual(jobs[0]["playlist_url"], "https://www.youtube.com/@LeoCui/videos")
        batch.add_urls(self.store, ["https://www.youtube.com/@Otro"], expand=expand, channel_limit=0)
        self.assertEqual(calls[-1], ("https://www.youtube.com/@Otro/videos", None))   # 0 = sin límite

    def test_read_urls_ignores_blanks_and_comments(self):
        text = f"# mi lista\n\n{url(IDS[0])}\n  {url(IDS[1])}  # comentario\n#{url(IDS[2])}\n"
        self.assertEqual(batch.read_urls(text.splitlines()), [url(IDS[0]), url(IDS[1])])


class TrackTests(unittest.TestCase):
    def track(self, translated=False):
        return [{"ext": "vtt", "url": "v"}, {"ext": "json3", "url": "u&tlang=es" if translated else "u"}]

    def test_prefers_manual_then_original_language_over_translations(self):
        english = {"language": "en-US", "automatic_captions": {
            "es": self.track(translated=True), "en": self.track(translated=True),
            "en-orig": self.track()}}
        self.assertEqual(ing.choose_track(english, ["es", "en"])[1:], ("subtitles_auto", "en"))
        self.assertEqual(ing.subtitle_origin(english, ["es", "en"]), ("subtitles_auto", "en"))
        manual = dict(english, subtitles={"es-419": self.track()})
        self.assertEqual(ing.choose_track(manual, ["es", "en"])[1:], ("subtitles_manual", "es-419"))
        german = {"language": "de", "automatic_captions": {"es": self.track(True), "de": self.track()}}
        self.assertEqual(ing.choose_track(german, ["es", "en"])[1:], ("subtitles_auto", "de"))
        only_translated = {"automatic_captions": {"es": self.track(True)}}
        self.assertEqual(ing.choose_track(only_translated, ["es", "en"])[1:], ("subtitles_auto", "es"))
        self.assertIsNone(ing.choose_track({"automatic_captions": {"es": [{"ext": "vtt"}]}}, ["es"]))

    def test_fetch_downloads_with_ytdlp_session_and_reports_failures(self):
        video = {"title": "T", "language": "en", "automatic_captions": {
            "es": self.track(translated=True), "en-orig": self.track()}}
        body = '{"events": [{"tStartMs": 1500, "segs": [{"utf8": "hello"}]}]}'
        seen = {}

        class FakeYDL:
            fail = False

            def __init__(self, opts):
                seen["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, video_url, download):
                return video

            def urlopen(self, track_url):
                seen["url"] = track_url
                if self.fail:
                    raise RuntimeError("HTTP Error 429: Too Many Requests")
                return io.BytesIO(body.encode())

        with mock.patch.dict("sys.modules", {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYDL)}):
            got, cues = ing.fetch_subtitles(url(IDS[0]), ["es", "en"], "/tmp", "safari")
            self.assertEqual((cues, seen["url"]), ([(1.5, "hello")], "u"))  # la pista original
            self.assertEqual(seen["opts"]["cookiesfrombrowser"], ("safari",))
            FakeYDL.fail = True
            with self.assertRaises(ing.SubtitleDownloadError) as caught:
                ing.fetch_subtitles(url(IDS[0]), ["es", "en"], "/tmp", None)
            self.assertEqual(caught.exception.info["title"], "T")
            self.assertEqual(batch.classify_error(caught.exception)[0], "blocked")


class RunTests(QueueBase):
    def test_order_priority_progress_and_pauses(self):
        for i, video_id in enumerate(IDS[:3]):
            self.ok(video_id, f"Título {i + 1}", n=2314 if i == 0 else 3)
        self.add(IDS[0], IDS[1])
        self.add(IDS[2], priority=5)
        summary = self.run_queue()
        self.assertEqual(self.world.fetches, [IDS[2], IDS[0], IDS[1]])
        self.assertEqual(self.lines, [
            "[1/3] Título 3 · ingesta ok · 3 cues · subtítulos automáticos",
            "[2/3] Título 1 · ingesta ok · 2.314 cues · subtítulos automáticos",
            "[3/3] Título 2 · ingesta ok · 3 cues · subtítulos automáticos"])
        self.assertEqual(self.world.sleeps, [5.0, 5.0])  # entre vídeos, no tras el último
        self.assertEqual((summary.ingested, summary.done, summary.failed, summary.remaining),
                         (3, 0, 0, 0))
        self.assertEqual(self.store.stats()["transcripts"], 3)
        self.assertEqual(batch.format_summary(summary),
                         "Resumen: 0 hechos · 3 ingeridos (pendientes de extracción) · "
                         "0 fallidos · 0 saltados · tiempo total 0:00:10")

    def test_limit_and_custom_delay(self):
        for video_id in IDS[:3]:
            self.ok(video_id)
        self.add(*IDS[:3])
        summary = self.run_queue(limit=2, delay=10)
        self.assertEqual(self.world.fetches, IDS[:2])
        self.assertEqual(self.world.sleeps, [8.0])  # 10 s ± 20 %
        self.assertEqual(summary.remaining, 1)
        self.lines.clear()
        self.run_queue(delay=0)
        self.assertEqual(self.lines[0][:5], "[1/1]")

    def test_one_failure_does_not_stop_the_batch(self):
        self.ok(IDS[0])
        self.world.videos[IDS[1]] = RuntimeError("ERROR: [youtube] bbbbbbbbbb2: Private video. "
                                                 "Sign in if you've been granted access")
        self.ok(IDS[2])
        self.add(*IDS[:3])
        summary = self.run_queue()
        self.assertEqual((summary.ingested, summary.failed), (2, 1))
        self.assertEqual(self.statuses()[IDS[1]], ("failed", "ingest"))
        self.assertEqual(self.lines[1], f"[2/3] {IDS[1]} · fallido · vídeo privado")
        self.assertEqual(self.world.fetches, IDS[:3])

    def test_permanent_error_is_not_retried(self):
        self.world.videos[IDS[0]] = RuntimeError("Video unavailable. This video has been removed "
                                                 "by the uploader")
        self.add(IDS[0])
        self.run_queue()
        job = self.store.list_jobs()[0]
        self.assertEqual((job["status"], job["attempts"], job["last_error"]),
                         ("failed", 1, "vídeo eliminado"))
        self.assertEqual((len(self.world.fetches), self.world.sleeps), (1, []))

    def test_transient_error_is_retried_with_growing_wait(self):
        self.world.videos[IDS[0]] = [
            urllib.error.URLError("timed out"),
            RuntimeError("HTTP Error 503: Service Unavailable"),
            (info("Al tercer intento"), [(0.0, "hola")])]
        self.add(IDS[0])
        summary = self.run_queue()
        self.assertEqual(self.world.sleeps, [30.0, 120.0])
        self.assertEqual((summary.ingested, summary.failed), (1, 0))
        job = self.store.list_jobs()[0]
        self.assertEqual((job["status"], job["stage"], job["attempts"]), ("pending", "extract", 3))
        self.assertIn("intento 1 fallido", self.lines[0])
        self.assertIn("reintento en 30 s", self.lines[0])
        self.assertIn("reintento en 120 s", self.lines[1])

    def test_transient_error_gives_up_after_three_attempts(self):
        self.world.videos[IDS[0]] = urllib.error.URLError("connection reset")
        self.add(IDS[0])
        summary = self.run_queue()
        job = self.store.list_jobs()[0]
        self.assertEqual((job["status"], job["attempts"], len(self.world.fetches)), ("failed", 3, 3))
        self.assertIn("error de red", job["last_error"])
        self.assertIn("(tras 3 intentos)", self.lines[-1])
        self.assertEqual(summary.failed, 1)

    def test_failed_subtitle_download_is_not_mistaken_for_no_subtitles(self):
        # yt_digest devuelve [] si la pista existe pero su descarga falla (p. ej. 429).
        self.world.videos[IDS[0]] = [(info("Con pista"), []), (info("Con pista"), [(0.0, "ya")])]
        self.add(IDS[0])
        self.run_queue(no_whisper=True)
        self.assertEqual(self.statuses()[IDS[0]], ("pending", "extract"))
        self.assertEqual((self.world.transcribed, self.world.sleeps), ([], [30.0]))

    def test_undownloadable_subtitles_fall_back_to_whisper_on_last_try(self):
        self.world.videos[IDS[0]] = (info("Pista bloqueada"), [])  # siempre vacía
        self.add(IDS[0])
        summary = self.run_queue()
        self.assertEqual((len(self.world.fetches), self.world.sleeps), (3, [30.0, 120.0]))
        self.assertEqual(self.world.transcribed, [IDS[0]])
        self.assertEqual(self.lines[-1], "[1/1] Pista bloqueada · ingesta ok · 1 cues · Whisper")
        self.assertEqual((summary.ingested, summary.failed), (1, 0))

    def test_undownloadable_subtitles_without_whisper_are_skipped_for_later(self):
        self.world.videos[IDS[0]] = ing.SubtitleDownloadError(info("Pista bloqueada"),
                                                              "HTTP Error 429: Too Many Requests")
        self.add(IDS[0])
        summary = self.run_queue(no_whisper=True)
        job = self.store.list_jobs()[0]
        self.assertEqual((job["status"], job["notes"]), ("skipped", batch.NO_SUBS_NOTE))
        self.assertEqual((summary.skipped, summary.failed, summary.blocked), (1, 0, False))
        self.assertIn("los subtítulos no se pudieron descargar", self.lines[-1])
        self.run_queue(whisper_only=True)
        self.assertEqual(self.world.transcribed, [IDS[0]])
        self.assertEqual(self.statuses()[IDS[0]], ("pending", "extract"))

    def test_extract_stage_only_touches_saved_videos(self):
        self.ok(IDS[0])
        self.ok(IDS[1])
        self.add(IDS[0], IDS[1])
        self.run_queue(limit=1)
        self.lines.clear()
        summary = self.run_queue(stage="extract", extractor=True)
        self.assertEqual((summary.done, summary.remaining), (1, 0))
        self.assertEqual(self.world.fetches, [IDS[0]])  # no ingiere el que faltaba
        self.assertEqual(self.statuses(), {IDS[0]: ("done", "extract"), IDS[1]: ("pending", "ingest")})

    def test_youtube_blocking_stops_the_batch_and_requeues(self):
        for video_id in IDS[:4]:
            self.world.videos[video_id] = RuntimeError("HTTP Error 429: Too Many Requests")
        self.add(*IDS[:4])
        summary = self.run_queue()
        self.assertTrue(summary.blocked)
        self.assertEqual(self.world.fetches.count(IDS[3]), 0)  # no se sigue insistiendo
        self.assertEqual(set(self.statuses().values()), {("pending", "ingest")})
        self.assertEqual((summary.failed, summary.remaining), (0, 4))
        self.assertIn("se detiene el lote", self.lines[-1])

    def test_recovers_running_jobs_after_crash(self):
        self.ok(IDS[0])
        self.ok(IDS[1])
        self.add(IDS[0], IDS[1])
        first = self.store.list_jobs()[0]["id"]
        self.store.update_job(first, status="running", started_at="2026-10-01T00:00:00Z")
        summary = self.run_queue()
        self.assertEqual(summary.recovered, 1)
        self.assertIn("Recuperados 1 trabajos", self.lines[0])
        self.assertEqual(self.world.fetches, [IDS[0], IDS[1]])
        self.assertEqual(set(self.statuses().values()), {("pending", "extract")})

    def test_no_whisper_skips_and_whisper_only_recovers(self):
        self.world.videos[IDS[0]] = (info("Sin subtítulos", subs=False), [])
        self.ok(IDS[1])
        self.add(IDS[0], IDS[1])
        summary = self.run_queue(no_whisper=True)
        self.assertEqual((summary.skipped, summary.ingested), (1, 1))
        job = self.store.list_jobs("skipped")[0]
        self.assertEqual((job["video_id"], job["notes"]),
                         (IDS[0], "sin subtítulos (pendiente de Whisper)"))
        self.assertEqual(self.world.transcribed, [])
        self.assertEqual(self.lines[0],
                         f"[1/2] {IDS[0]} · saltado · sin subtítulos (pendiente de Whisper)")
        self.assertEqual(self.store.stats()["sources"], 1)  # no queda una fuente a medias

        self.assertEqual(self.run_queue().ingested, 0)  # una pasada normal no los toca
        self.lines.clear()
        summary = self.run_queue(whisper_only=True)
        self.assertEqual(self.world.transcribed, [IDS[0]])
        self.assertEqual(self.lines, ["[1/1] Sin subtítulos · ingesta ok · 1 cues · Whisper"])
        job = self.store.list_jobs()[0]
        self.assertEqual((job["status"], job["stage"], job["notes"]), ("pending", "extract", None))

    def test_ctrl_c_leaves_current_job_pending_and_reports(self):
        self.ok(IDS[0])
        self.world.videos[IDS[1]] = KeyboardInterrupt()
        self.ok(IDS[2])
        self.add(*IDS[:3])
        summary = self.run_queue()
        self.assertTrue(summary.interrupted)
        self.assertEqual((summary.ingested, summary.remaining), (1, 2))
        self.assertEqual(self.statuses(), {IDS[0]: ("pending", "extract"),
                                           IDS[1]: ("pending", "ingest"),
                                           IDS[2]: ("pending", "ingest")})
        job = self.store.list_jobs()[1]
        self.assertEqual((job["attempts"], job["started_at"]), (0, None))
        self.assertEqual(self.store.stats()["transcripts"], 1)
        self.assertIn("Interrumpido", self.lines[-1])
        # Reanudar termina el resto.
        self.ok(IDS[1])
        self.assertEqual(self.run_queue().ingested, 2)

    def test_ctrl_c_during_pause_is_clean(self):
        self.ok(IDS[0])
        self.ok(IDS[1])
        self.add(IDS[0], IDS[1])
        self.world.sleep = mock.Mock(side_effect=KeyboardInterrupt())
        summary = self.run_queue()
        self.assertTrue(summary.interrupted)
        self.assertEqual(self.statuses(), {IDS[0]: ("pending", "extract"),
                                           IDS[1]: ("pending", "ingest")})

    def test_without_extractor_only_ingests_and_leaves_job_ready(self):
        self.ok(IDS[0], "Solo ingesta")
        self.add(IDS[0])
        summary = self.run_queue(stage="all", extractor=False)
        self.assertEqual((summary.done, summary.ingested, summary.failed), (0, 1, 0))
        self.assertEqual(self.statuses()[IDS[0]], ("pending", "extract"))
        self.assertIn("extracción pendiente (aún no disponible)", self.lines[0])
        with mock.patch.dict("sys.modules", {"rezme.extract": None}):
            self.assertIsNone(batch.load_extractor("claude-code"))

    def test_stage_all_extracts_and_marks_done(self):
        self.ok(IDS[0], "Completo")
        source, _ = self.store.add_source("youtube", IDS[1], title="Ya ingerido")
        self.store.save_transcript(source, [(0.0, "hola")], "subtitles_auto")
        self.add(IDS[0], IDS[1])
        summary = self.run_queue(stage="all", extractor=True)
        self.assertEqual((summary.done, summary.ingested), (2, 0))
        self.assertEqual(self.world.fetches, [IDS[0]])  # el ya ingerido no se descarga
        self.assertEqual(self.lines[0], "[1/2] Completo · ingesta ok · 3 cues · subtítulos "
                                        "automáticos · extracción ok · 12 afirmaciones "
                                        "(10 verificadas)")
        self.assertEqual(self.lines[1], "[2/2] Ya ingerido · extracción ok · 12 afirmaciones "
                                        "(10 verificadas)")
        self.assertEqual(set(self.statuses().values()), {("done", "extract")})
        self.assertEqual(self.run_queue(stage="ingest").ingested, 0)

    def test_extraction_failure_retries_only_the_extraction(self):
        self.ok(IDS[0])
        self.add(IDS[0])
        outcomes = [SimpleNamespace(chunks_failed=2, claims_new=0, verified=0),
                    SimpleNamespace(chunks_failed=0, claims_new=4, verified=4)]
        deps = self.world.deps()
        deps.extractor = lambda store, source_id: outcomes.pop(0)
        summary = batch.run_queue(self.store, stage="all", deps=deps, out=self.lines.append)
        self.assertEqual((summary.done, len(self.world.fetches)), (1, 1))
        self.assertIn("2 tramos fallaron", self.lines[0])

    def test_management(self):
        self.ok(IDS[0])
        self.world.videos[IDS[1]] = RuntimeError("Private video")
        self.world.videos[IDS[2]] = (info("Sin subs", subs=False), [])
        self.add(*IDS[:3])
        self.run_queue(stage="all", extractor=True, no_whisper=True)
        self.assertEqual(self.store.job_counts(),
                         {"done/extract": 1, "failed/ingest": 1, "skipped/ingest": 1})
        self.assertEqual(self.store.retry_jobs(status="failed"), 1)
        job = self.store.list_jobs("pending")[0]
        self.assertEqual((job["attempts"], job["last_error"]), (0, None))
        self.assertEqual(self.store.retry_jobs(status="skipped"), 1)
        self.assertEqual(self.store.retry_jobs(job_id=999), 0)
        self.assertTrue(self.store.remove_job(job["id"]))
        self.assertFalse(self.store.remove_job(job["id"]))

    def test_clear_done_never_touches_stored_data(self):
        self.ok(IDS[0])
        self.ok(IDS[1])
        self.add(IDS[0], IDS[1])
        self.run_queue(stage="all", extractor=True, limit=1)
        before = self.store.stats()
        self.assertEqual(self.store.clear_done_jobs(), 1)
        after = self.store.stats()
        self.assertEqual(after["jobs"], before["jobs"] - 1)
        for table in ("sources", "transcripts", "claims", "extraction_runs"):
            self.assertEqual(after[table], before[table], table)
        self.assertEqual(after["transcripts"], 1)
        self.assertEqual(self.statuses(), {IDS[1]: ("pending", "ingest")})

    def test_errors_are_sanitized_before_being_stored(self):
        self.world.videos[IDS[0]] = ValueError(
            "\x1b[0;31mERROR:\x1b[0m fallo raro en "
            "https://rr1.googlevideo.com/videoplayback?sig=FIRMASECRETA&cookie=abc\n" + "x" * 900)
        self.add(IDS[0])
        self.run_queue(cookies_from="chrome")
        error = self.store.list_jobs()[0]["last_error"]
        self.assertLessEqual(len(error), 300)
        self.assertTrue(error.startswith("fallo raro en https://rr1.googlevideo.com/videoplayback x"))
        dump = "\n".join(self.store.db.iterdump()) + "\n".join(self.lines)
        for secret in ("FIRMASECRETA", "cookie=abc", "chrome"):
            self.assertNotIn(secret, dump)

    def test_classify_error(self):
        cases = {
            "Sign in to confirm your age. This video may be inappropriate": "permanent",
            "Join this channel to get access to members-only content": "permanent",
            "Sign in to confirm you’re not a bot": "blocked",
            "HTTP Error 429: Too Many Requests": "blocked",
            "HTTP Error 502: Bad Gateway": "transient",
            "algo inesperado": "permanent",
        }
        for message, kind in cases.items():
            self.assertEqual(batch.classify_error(RuntimeError(message))[0], kind, message)
        self.assertEqual(batch.classify_error(TimeoutError())[0], "transient")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "k.db")
        self.world = World()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_full_cycle(self):
        expand = lambda playlist_url, cookies: [
            batch.PlaylistEntry(IDS[0], "Uno"), batch.PlaylistEntry(IDS[1], "[Private video]",
                                                                   "vídeo privado")]
        with mock.patch.object(batch, "expand_playlist", expand):
            code, out, _ = self.cli("queue", "add", PLAYLIST)
        self.assertEqual((code, out.splitlines()[0]),
                         (0, "✓ 1 añadidos · 0 ya estaban · 1 no accesibles"))
        self.assertIn("✗ [Private video]: vídeo privado", out)

        urls = Path(self.tmp.name) / "urls.txt"
        urls.write_text(f"# pendientes\n{url(IDS[0])}\n\n{url(IDS[2])}\n", encoding="utf-8")
        code, out, _ = self.cli("queue", "add", "--file", str(urls), "--priority", "3")
        self.assertIn("1 añadidos · 1 ya estaban", out)
        code, out, _ = self.cli("queue", "add", "--stdin", stdin=f"{url(IDS[3])}\n#x\n")
        self.assertIn("1 añadidos", out)
        self.assertEqual(self.cli("queue", "add")[0], 1)
        code, _, err = self.cli("queue", "add", "https://example.com/x")
        self.assertEqual(code, 1)
        self.assertIn("URL no válida", err)

        self.world.videos = {
            IDS[0]: (info("Uno"), [(0.0, "a")]),
            IDS[2]: RuntimeError("Private video"),
            IDS[3]: (info("Cuatro", subs=False), [])}
        deps = self.world.deps()
        with mock.patch.object(batch, "Deps", lambda: deps):
            code, out, _ = self.cli("queue", "run", "--no-whisper", "--delay", "0")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"[1/3] {IDS[2]} · fallido · vídeo privado")  # prioridad 3
        self.assertEqual(lines[1], "[2/3] Uno · ingesta ok · 1 cues · subtítulos automáticos")
        self.assertIn("saltado", lines[2])
        self.assertEqual(lines[3], "Resumen: 0 hechos · 1 ingeridos (pendientes de extracción) · "
                                   "1 fallidos · 1 saltados · tiempo total 0:00:00")

        code, out, _ = self.cli("queue", "status")
        self.assertIn("pendientes: 0 de ingesta · 1 de extracción", out)
        self.assertIn("fallidos: 1", out)
        self.assertIn("Últimos errores:", out)
        self.assertIn("vídeo privado", out)
        code, out, _ = self.cli("queue", "list", "--status", "skipped")
        self.assertIn("sin subtítulos (pendiente de Whisper)", out)

        self.assertEqual(self.cli("queue", "retry")[0], 1)
        self.assertIn("1 trabajos vuelven", self.cli("queue", "retry", "--failed")[1])
        self.assertIn("1 trabajos vuelven", self.cli("queue", "retry", "--skipped")[1])
        self.assertEqual(self.cli("queue", "remove", "999")[0], 1)
        self.assertEqual(self.cli("queue", "clear")[0], 1)
        self.assertIn("0 trabajos terminados", self.cli("queue", "clear", "--done")[1])
        with Store(self.db) as store:
            self.assertEqual(store.stats()["transcripts"], 1)

    def test_run_all_without_backend_reports_the_problem(self):
        with mock.patch("rezme.backends.check_backend", return_value="No encuentro el CLI `claude` en el PATH."):
            code, _, err = self.cli("queue", "run", "--stage", "all")
        self.assertEqual((code, err.strip()), (1, "No encuentro el CLI `claude` en el PATH."))

    def test_interrupted_run_returns_130(self):
        self.cli("queue", "add", url(IDS[0]))
        self.world.videos[IDS[0]] = KeyboardInterrupt()
        deps = self.world.deps()
        with mock.patch.object(batch, "Deps", lambda: deps):
            code, out, _ = self.cli("queue", "run")
        self.assertEqual(code, 130)
        self.assertIn("Interrumpido", out)
        self.assertIn("Resumen:", out)


if __name__ == "__main__":
    unittest.main()
