import sqlite3
import unittest

from rezme import AmbiguousEntity, Claim, Implication, Store, ValidationError
from rezme import ingest as ing
from rezme.schema import default_expires_at, normalize_name, parse_ts


class SchemaTests(unittest.TestCase):
    def claim(self, **changes):
        base = dict(source_id=1, statement="Una idea.", type="fact")
        base.update(changes)
        return Claim(**base)

    def test_rejects_unknown_vocabulary(self):
        for field, value in (("type", "rumor"), ("evidence_grade", "vibes"),
                             ("stance", "moon"), ("status", "ok")):
            with self.assertRaises(ValidationError, msg=field):
                self.claim(**{field: value}).validate()

    def test_rejects_bad_values(self):
        for changes in (dict(statement="  "), dict(confidence=1.5), dict(confidence=True),
                        dict(metric_value=float("nan")), dict(ts_start=10, ts_end=5),
                        dict(as_of="mañana"), dict(quote="x" * 301),
                        dict(valid_from="2026-05-01", valid_to="2026-01-01")):
            with self.assertRaises(ValidationError, msg=str(changes)):
                self.claim(**changes).validate()

    def test_accepts_partial_dates_and_clock_timestamps(self):
        c = self.claim(as_of="2026-09", valid_from="2026", ts_start="01:02:03", ts_end="[01:02:10]")
        c.validate()
        self.assertEqual(c.ts_start, 3723.0)
        self.assertEqual(c.ts_end, 3730.0)

    def test_parse_ts(self):
        self.assertEqual(parse_ts("02:01:14"), 7274.0)
        self.assertEqual(parse_ts("1:30"), 90.0)
        with self.assertRaises(ValidationError):
            parse_ts("abc")

    def test_normalize_name(self):
        self.assertEqual(normalize_name("  AST  SpaceMobile "), "ast spacemobile")
        self.assertEqual(normalize_name("Inflación"), normalize_name("inflacion"))

    def test_expiry_defaults_by_type(self):
        self.assertEqual(default_expires_at("opinion", "2026-01-01"), "2026-04-01T00:00:00Z")
        self.assertIsNone(default_expires_at("methodology", "2026-01-01"))
        self.assertEqual(default_expires_at("forecast", "2026-01-01", "2026-06-30"),
                         "2026-06-30T00:00:00Z")

    def test_implication_needs_a_target(self):
        with self.assertRaises(ValidationError):
            Implication(claim_id=1, direction="positive", basis="inferred_by_system").validate()
        with self.assertRaises(ValidationError):
            Implication(claim_id=1, direction="up", basis="inferred_by_system",
                        target_label="bonos").validate()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.src, _ = self.store.add_source("youtube", "fYoi6OjmIlw", title="Hablemos de $ASTS",
                                            published_at="2026-09-01")

    def tearDown(self):
        self.store.close()

    def add(self, statement="Eficiencia espectral estimada de 1,36 bps/Hz", **changes):
        base = dict(source_id=self.src, statement=statement, type="own_calculation",
                    status="verified")
        base.update(changes)
        return self.store.add_claim(Claim(**base))

    def test_source_upsert_keeps_existing_data(self):
        again, created = self.store.add_source("youtube", "fYoi6OjmIlw", channel="Emérito")
        self.assertEqual((again, created), (self.src, False))
        row = self.store.get_source("youtube", "fYoi6OjmIlw")
        self.assertEqual(row["title"], "Hablemos de $ASTS")
        self.assertEqual(row["channel"], "Emérito")

    def test_transcript_is_idempotent_and_versioned(self):
        cues = [(0.0, "hola"), (30.5, "mundo")]
        t1, new1 = self.store.save_transcript(self.src, cues, "subtitles_auto", "es")
        t2, new2 = self.store.save_transcript(self.src, cues, "subtitles_auto", "es")
        self.assertEqual((t1, new1, t2, new2), (t1, True, t1, False))
        t3, new3 = self.store.save_transcript(self.src, cues + [(60, "fin")], "whisper", "es")
        self.assertTrue(new3)
        self.assertNotEqual(t3, t1)
        latest = self.store.latest_transcript(self.src)
        self.assertEqual(latest["id"], t3)
        self.assertEqual(latest["cues"][1], (30.5, "mundo"))

    def test_transcript_rejects_empty_and_bad_origin(self):
        with self.assertRaises(ValidationError):
            self.store.save_transcript(self.src, [], "whisper")
        with self.assertRaises(ValidationError):
            self.store.save_transcript(self.src, [(0, "x")], "magia")

    def test_entity_aliases_resolve_across_case_and_accents(self):
        a = self.store.upsert_entity("company", "AST SpaceMobile", ["ASTS", "AST"],
                                     {"ticker": "ASTS"})
        b = self.store.upsert_entity("company", "ast spacemobile", ["$ASTS"], {"exchange": "NASDAQ"})
        self.assertEqual(a, b)
        self.assertEqual(self.store.resolve_entity("asts"), a)
        entity = self.store.get_entity(a)
        self.assertEqual(entity["external_ids"], {"ticker": "ASTS", "exchange": "NASDAQ"})
        self.assertIsNone(self.store.resolve_entity("Tesla"))

    def test_ambiguous_entity_is_never_guessed(self):
        self.store.upsert_entity("company", "Mercury Systems", ["mercury"])
        self.store.upsert_entity("biological_concept", "Mercurio", ["mercury"])
        with self.assertRaises(AmbiguousEntity):
            self.store.resolve_entity("mercury")
        self.assertIsNotNone(self.store.resolve_entity("mercury", type="company"))

    def test_claim_roundtrip_and_accent_insensitive_search(self):
        entity = self.store.upsert_entity("company", "AST SpaceMobile", ["ASTS"])
        cid, created = self.add(entity_id=entity, domain="equities", metric_name="spectral_eff",
                                metric_value=1.36, metric_unit="bps/Hz", ts_start="02:01:14",
                                attrs={"band": "low"})
        self.assertTrue(created)
        hits = self.store.search_claims("ESPECTRAL eficiencia")
        self.assertEqual([h["id"] for h in hits], [cid])
        hit = hits[0]
        self.assertEqual(hit["entity_name"], "AST SpaceMobile")
        self.assertEqual(hit["ts_start"], 7274.0)
        self.assertEqual(hit["attrs"], {"band": "low"})

    def test_agents_only_get_verified_claims(self):
        cid, _ = self.add(status="candidate")
        self.assertEqual(self.store.search_claims("espectral"), [])
        self.assertEqual(len(self.store.search_claims("espectral", status=None)), 1)
        self.store.set_claim_status(cid, "verified")
        self.assertEqual(len(self.store.search_claims("espectral")), 1)
        self.store.set_claim_status(cid, "rejected")
        self.assertEqual(self.store.search_claims("espectral"), [])
        with self.assertRaises(ValidationError):
            self.store.set_claim_status(cid, "ok")
        with self.assertRaises(KeyError):
            self.store.set_claim_status(9999, "verified")

    def test_expiry_and_point_in_time(self):
        self.add("El precio objetivo es alto", type="opinion", published_at="2026-01-01")
        q = "precio objetivo"
        self.assertEqual(len(self.store.search_claims(q, known_at="2026-02-01T00:00:00Z")), 1)
        self.assertEqual(self.store.search_claims(q, known_at="2026-06-01T00:00:00Z"), [])
        self.assertEqual(len(self.store.search_claims(q, known_at="2026-06-01T00:00:00Z",
                                                      include_expired=True)), 1)
        # No existía todavía el 2025-12-01: no puede filtrarse al pasado.
        self.assertEqual(self.store.search_claims(q, known_at="2025-12-01T00:00:00Z",
                                                  include_expired=True), [])

    def test_methodology_never_expires(self):
        self.add("Regla 80/10 de asignación", type="methodology", published_at="2020-01-01")
        self.assertEqual(len(self.store.search_claims("asignación")), 1)

    def test_same_run_dedupes_but_new_run_keeps_history(self):
        run1 = self.store.start_run(model="m", backend="api", prompt_version="v1")
        run2 = self.store.start_run(model="m", backend="api", prompt_version="v2")
        a, c1 = self.add(run_id=run1)
        b, c2 = self.add(run_id=run1)
        c, c3 = self.add(run_id=run2)
        self.assertEqual((a, c1, b, c2), (a, True, a, False))
        self.assertTrue(c3)
        self.assertNotEqual(a, c)

    def test_unknown_source_and_foreign_keys(self):
        with self.assertRaises(ValidationError):
            self.store.add_claim(Claim(source_id=999, statement="x", type="fact"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_implication(Implication(
                claim_id=999, direction="positive", basis="stated_by_source", target_label="oro"))

    def test_implications_keep_basis(self):
        cid, _ = self.add("Ensayo fase 3 con reducción de peso del 15 %", type="study_result",
                          domain="medicine", evidence_grade="peer_reviewed")
        pharma = self.store.upsert_entity("company", "Novo Nordisk")
        self.store.add_implication(Implication(
            claim_id=cid, direction="positive", basis="inferred_by_system",
            target_entity_id=pharma, mechanism="Mayor demanda esperada", confidence=0.4))
        self.store.add_implication(Implication(
            claim_id=cid, direction="negative", basis="stated_by_source",
            target_label="snacks y comida rápida"))
        bases = [i["basis"] for i in self.store.implications_for(cid)]
        self.assertEqual(bases, ["inferred_by_system", "stated_by_source"])

    def test_db_rejects_invalid_values_even_bypassing_python(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute(
                "INSERT INTO claims (source_id, type, statement, evidence_grade, stance, status,"
                " captured_at, fingerprint) VALUES (?, 'rumor', 'x', 'none', 'n/a', 'candidate',"
                " 'now', 'f')", (self.src,))

    def test_stats(self):
        self.add()
        stats = self.store.stats()
        self.assertEqual(stats["claims"], 1)
        self.assertEqual(stats["claims_by_status"], {"verified": 1})

    def test_reopens_existing_file(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "k.db")
            with Store(path) as s:
                s.add_source("youtube", "abc12345678")
            with Store(path) as s:
                self.assertEqual(s.stats()["sources"], 1)


URL = "https://www.youtube.com/watch?v=fYoi6OjmIlw&t=2018s"
INFO = {"title": "Hablemos de $ASTS", "channel": "Emérito Quintana", "channel_id": "UC1",
        "upload_date": "20260901", "duration": 14252,
        "chapters": [{"start_time": 0, "title": "Intro"}],
        "automatic_captions": {"es": [{"ext": "json3", "url": "u"}]}}


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.fetch_calls = 0

    def tearDown(self):
        self.store.close()

    def fetch(self, cues, info=INFO):
        def _fetch(url, langs, tmp, cookies):
            self.fetch_calls += 1
            return dict(info), list(cues)
        return _fetch

    def test_video_id_parsing(self):
        ok = {
            URL: "fYoi6OjmIlw",
            "https://youtu.be/JXiQ6Tk_P84?t=5": "JXiQ6Tk_P84",
            "https://www.youtube.com/shorts/JXiQ6Tk_P84": "JXiQ6Tk_P84",
            "https://m.youtube.com/watch?v=JXiQ6Tk_P84&pp=x": "JXiQ6Tk_P84",
        }
        for url, expected in ok.items():
            self.assertEqual(ing.video_id_from_url(url), expected, url)
        for bad in ("https://example.com/watch?v=fYoi6OjmIlw", "ftp://youtube.com/watch?v=fYoi6OjmIlw",
                    "https://youtube.com/watch?v=corto", "https://user@youtube.com/watch?v=fYoi6OjmIlw",
                    "no es una url", "https://www.youtube.com/"):
            self.assertIsNone(ing.video_id_from_url(bad), bad)

    def test_persists_raw_cues_and_metadata(self):
        res = ing.ingest_url(self.store, URL, fetch=self.fetch([(0.0, "hola"), (12.5, "adiós")]))
        self.assertFalse(res.skipped_download)
        self.assertEqual((res.origin, res.n_cues), ("subtitles_auto", 2))
        source = self.store.get_source("youtube", "fYoi6OjmIlw")
        self.assertEqual(source["published_at"], "2026-09-01")
        self.assertEqual(source["channel"], "Emérito Quintana")
        self.assertIn("Intro", source["chapters_json"])
        self.assertEqual(self.store.latest_transcript(source["id"])["cues"][1], (12.5, "adiós"))

    def test_second_ingest_does_not_download_again(self):
        fetch = self.fetch([(0.0, "hola")])
        ing.ingest_url(self.store, URL, fetch=fetch)
        again = ing.ingest_url(self.store, URL, fetch=fetch)
        self.assertEqual(self.fetch_calls, 1)
        self.assertTrue(again.skipped_download)
        forced = ing.ingest_url(self.store, URL, force=True, fetch=fetch)
        self.assertEqual(self.fetch_calls, 2)
        self.assertFalse(forced.new_transcript)  # mismo contenido: no se duplica

    def test_manual_subtitles_are_labelled(self):
        info = dict(INFO, subtitles={"es": [{"ext": "json3", "url": "u"}]})
        res = ing.ingest_url(self.store, URL, fetch=self.fetch([(0, "x")], info))
        self.assertEqual(res.origin, "subtitles_manual")

    def test_falls_back_to_whisper_without_subtitles(self):
        calls = []
        def transcribe(url, tmp, model, lang, cookies):
            calls.append((model, lang))
            return [(0.0, "texto de whisper")]
        res = ing.ingest_url(self.store, URL, fetch=self.fetch([]), transcribe=transcribe)
        self.assertEqual(res.origin, "whisper")
        self.assertEqual(calls, [("small", "es")])

    def test_fails_clearly_without_any_text(self):
        with self.assertRaisesRegex(RuntimeError, "transcripción"):
            ing.ingest_url(self.store, URL, fetch=self.fetch([]), transcribe=lambda *a: [])
        self.assertEqual(self.store.stats()["sources"], 0)

    def test_invalid_url_does_not_touch_network(self):
        with self.assertRaisesRegex(ValueError, "URL"):
            ing.ingest_url(self.store, "https://example.com/x",
                           fetch=lambda *a: self.fail("no debe descargar"))


if __name__ == "__main__":
    unittest.main()
