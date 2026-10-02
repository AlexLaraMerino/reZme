import json
import unittest

from rezme import Claim, Store, ValidationError, backends, calibration


class CalibrationBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.src = {}
        for name, video, channel, published in (("vic", "aaaaaaaaaa1", "VIC", "2026-06-07"),
                                                ("pod", "bbbbbbbbbb2", "POD", "2026-09-18"),
                                                ("vic2", "cccccccccc3", "VIC", "2026-09-20")):
            self.src[name], _ = self.store.add_source("youtube", video, title=f"Vídeo {name}",
                                                      channel=channel.title(), channel_id=channel,
                                                      published_at=published)
        self.fed = self.store.upsert_entity("central_bank", "Reserva Federal")

    def tearDown(self):
        self.store.close()

    def claim(self, source, statement, type="fact", **changes):
        base = dict(source_id=self.src[source], statement=statement, type=type, status="verified",
                    entity_id=self.fed)
        base.update(changes)
        return self.store.add_claim(Claim(**base))[0]


class TargetDateTests(unittest.TestCase):
    def test_precise_horizons_give_a_date_and_vague_ones_do_not(self):
        cases = {("2027", "2026-09-01"): "2027-12-31", ("2026-12", "2026-07-01"): "2026-12-31",
                 ("2026-11-15", "2026-07-01"): "2026-11-15", ("fiscal 2027", "2026-07-01"): "2027-12-31",
                 ("12 meses", "2026-09-11"): "2027-09-11", ("18 meses", "2026-08-31"): "2028-02-29",
                 ("3 a 5 años", "2026-07-01"): "2031-07-01", ("3y", "2026-07-01"): "2029-07-01",
                 ("6 semanas", "2026-09-01"): "2026-10-16", ("este año", "2026-07-01"): "2026-12-31",
                 ("2 quarters", "2026-01-31"): "2026-07-31"}
        for (horizon, published), expected in cases.items():
            self.assertEqual(calibration.target_date(horizon, published), expected, horizon)
        for vague in ("largo plazo", "long-term", "próximos años", "meses", "2027+", "beyond 2027",
                      "mid-2030s", "a partir de 2028", "", None, "2026-13", "12 meses sin fecha de publicación"):
            self.assertIsNone(calibration.target_date(vague, None if vague and "sin fecha" in vague else "2026-07-01")
                              if vague != "12 meses sin fecha de publicación"
                              else calibration.target_date("12 meses", None), vague)


class LedgerTests(CalibrationBase):
    def test_sync_resolve_and_due(self):
        hike = self.claim("vic", "La Fed subirá los tipos en septiembre.", "forecast", horizon="2026-09")
        vague = self.claim("vic", "La IA cambiará la economía.", "forecast", horizon="largo plazo")
        later = self.claim("vic", "Habrá recesión.", "forecast", horizon="2028")
        self.claim("vic", "No es una previsión.", "opinion")
        self.store.add_claim(Claim(source_id=self.src["vic"], statement="Sin verificar.", type="forecast",
                                   status="ungrounded"))
        self.assertEqual(calibration.sync_forecasts(self.store), 3)
        self.assertEqual(calibration.sync_forecasts(self.store), 0)   # no duplica
        rows = {f["claim_id"]: f for f in calibration.list_forecasts(self.store, today="2026-10-02")}
        self.assertEqual({k: (v["target_date"], v["due"]) for k, v in rows.items()},
                         {hike: ("2026-09-30", True), later: ("2028-12-31", False), vague: (None, False)})
        self.assertEqual(rows[hike]["channel"], "Vic")

        fact = self.claim("pod", "La Fed subió 25 puntos básicos.")
        calibration.resolve(self.store, hike, "correct", notes="Subió el 16 de septiembre", evidence=[fact, fact, "x"])
        row = {f["claim_id"]: f for f in calibration.list_forecasts(self.store, today="2026-10-02")}[hike]
        self.assertEqual((row["resolution"], row["due"], row["evidence"], row["notes"]),
                         ("correct", False, [fact], "Subió el 16 de septiembre"))
        self.assertIsNotNone(row["resolved_at"])
        calibration.resolve(self.store, hike, "pending")              # se puede deshacer
        self.assertIsNone(calibration.list_forecasts(self.store)[0]["resolved_at"])
        with self.assertRaises(ValidationError):
            calibration.resolve(self.store, hike, "acertó")
        with self.assertRaises(KeyError):
            calibration.resolve(self.store, 99999, "correct")

    def test_profiles_measure_hit_rate_per_channel(self):
        ok = self.claim("vic", "Subirá tipos.", "forecast", horizon="2026-09")
        bad = self.claim("vic", "Caerá el oro.", "forecast", horizon="2026-09")
        half = self.claim("vic2", "Subirá la bolsa un 10 %.", "forecast", horizon="2026-12")
        self.claim("vic", "Seguirá el ciclo.", "forecast", horizon="2026-08")
        self.claim("pod", "Bitcoin doblará.", "forecast", horizon="largo plazo")
        mech = self.claim("pod", "Los tipos altos son estímulo.", "mechanism")
        other = self.claim("vic", "Los tipos altos enfrían.", "mechanism")
        self.store.add_claim(Claim(source_id=self.src["pod"], statement="Inventada.", type="fact", status="ungrounded"))
        self.store.add_relation(mech, "contradicts", other)
        calibration.sync_forecasts(self.store)
        calibration.resolve(self.store, ok, "correct")
        calibration.resolve(self.store, bad, "incorrect")
        calibration.resolve(self.store, half, "partial")
        profiles = {p["channel"]: p for p in calibration.channel_profiles(self.store, today="2026-10-02")}
        vic, pod = profiles["Vic"], profiles["Pod"]
        self.assertEqual((vic["videos"], vic["forecasts"], vic["resolved"], vic["pending"], vic["due"]), (2, 4, 3, 1, 1))
        self.assertEqual(vic["hit_rate"], 0.5)          # 1 acierto + medio de 3
        self.assertEqual((pod["forecasts"], pod["resolved"], pod["hit_rate"]), (1, 0, None))  # sin datos, sin nota
        self.assertEqual((pod["verified"], pod["ungrounded"], pod["grounding"], pod["durable"]), (2, 1, 0.667, 1))
        self.assertEqual((vic["contradicted"], pod["contradicted"], vic["supported"]), (1, 1, 0))
        saved = json.loads(self.store.db.execute(
            "SELECT metrics_json FROM source_profiles WHERE channel_id='VIC'").fetchone()[0])
        self.assertEqual(saved["hit_rate"], 0.5)
        calibration.channel_profiles(self.store)        # recalcular actualiza, no duplica
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM source_profiles").fetchone()[0], 2)


class SuggestionTests(CalibrationBase):
    def test_model_suggestions_need_later_evidence_and_never_resolve(self):
        hike = self.claim("vic", "La Fed subirá los tipos en septiembre.", "forecast", horizon="2026-09")
        late = self.claim("vic2", "La Fed bajará tipos en 2027.", "forecast", horizon="2027")   # nada posterior
        fact = self.claim("pod", "La Fed subió 25 puntos básicos.")
        before = self.claim("vic", "Dato del mismo día que la previsión.")
        calibration.sync_forecasts(self.store)
        batches = calibration.suggestion_batches(self.store)
        self.assertEqual([(b["entity"], [f["claim_id"] for f in b["forecasts"]]) for b in batches],
                         [("Reserva Federal", [hike])])
        seen = []

        def model(system, user):
            seen.append((system, user))
            return json.dumps({"resolutions": [
                {"id": hike, "resolution": "correct", "evidence": [fact], "reason": "subió"},
                {"id": late, "resolution": "incorrect", "evidence": [fact]},        # no se le preguntó
                {"id": hike, "resolution": "seguro", "evidence": [fact]},            # resolución inventada
                {"id": 9999, "resolution": "correct", "evidence": [fact]}, "basura"]})

        result = calibration.suggest(self.store, model)
        self.assertEqual(result, {"entidades": 1, "sugerencias": 1})
        system, user = seen[0]
        self.assertIn("No uses tu conocimiento del mundo", system)
        self.assertIn("<afirmaciones_posteriores>", user)
        self.assertNotIn("Dato del mismo día", user)     # no es posterior
        row = {f["claim_id"]: f for f in calibration.list_forecasts(self.store)}[hike]
        self.assertEqual((row["resolution"], row["suggestion"]),
                         ("pending", {"resolution": "correct", "evidence": [fact], "reason": "subió"}))
        self.assertEqual(calibration.channel_profiles(self.store)[0]["resolved"], 0)   # sugerir no puntúa
        self.assertEqual(calibration.suggest(self.store, model)["entidades"], 0)       # ya tiene sugerencia
        calibration.resolve(self.store, hike, "correct", evidence=row["suggestion"]["evidence"])
        self.assertIsNone(calibration.list_forecasts(self.store)[0]["suggestion"])
        self.assertEqual(before, before)

    def test_evidence_must_be_later_than_the_forecast_and_from_the_list(self):
        hike = self.claim("pod", "La Fed volverá a subir.", "forecast", horizon="2026-12")
        old = self.claim("vic", "Dato anterior a la previsión.")
        new = self.claim("vic2", "La Fed mantuvo los tipos.")
        calibration.sync_forecasts(self.store)
        answer = lambda evidence: (lambda s, u: json.dumps({"resolutions": [
            {"id": hike, "resolution": "incorrect", "evidence": evidence}]}))
        for evidence in ([old], [424242], [], "nada"):
            self.assertEqual(calibration.suggest(self.store, answer(evidence))["sugerencias"], 0, evidence)
        self.assertEqual(calibration.suggest(self.store, answer([old, new]))["sugerencias"], 1)
        self.assertEqual(calibration.list_forecasts(self.store)[0]["suggestion"]["evidence"], [new])

    def test_access_errors_stop_and_other_failures_do_not(self):
        self.claim("vic", "Previsión.", "forecast", horizon="2026-09")
        self.claim("pod", "Hecho posterior.")
        calibration.sync_forecasts(self.store)

        def broken(system, user):
            raise RuntimeError("timed out")

        self.assertEqual(calibration.suggest(self.store, broken)["sugerencias"], 0)

        def denied(system, user):
            raise backends.BackendUnavailable("La clave API no es válida.")

        with self.assertRaises(backends.BackendUnavailable):
            calibration.suggest(self.store, denied)
        self.assertEqual(calibration.suggest(self.store, lambda s, u: "no es JSON")["sugerencias"], 0)


if __name__ == "__main__":
    unittest.main()
