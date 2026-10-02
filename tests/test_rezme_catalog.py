import json
import os
import tempfile
import unittest

from rezme import Claim, Implication, Store, ValidationError, catalog


class CatalogBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.src, _ = self.store.add_source("youtube", "aaaaaaaaaa1", title="Vídeo", published_at="2026-09-01")

    def tearDown(self):
        self.store.close()

    def entity(self, type_, name, claims=0, aliases=(), **ids):
        entity_id = self.store.upsert_entity(type_, name, aliases, ids)
        for i in range(claims):
            self.store.add_claim(Claim(source_id=self.src, statement=f"{name} dato {i}", type="fact",
                                       status="verified", entity_id=entity_id))
        return entity_id

    def names(self):
        return sorted(e["canonical_name"] for e in self.store.list_entities())


class MergeTests(CatalogBase):
    def test_merge_moves_claims_implications_aliases_and_logs(self):
        fed = self.entity("central_bank", "Reserva Federal", claims=2, aliases=["Fed"], pais="US")
        english = self.entity("central_bank", "Federal Reserve", claims=3, aliases=["FOMC"], fred="FEDFUNDS")
        claim, _ = self.store.add_claim(Claim(source_id=self.src, statement="Otra", type="fact", status="verified"))
        self.store.add_implication(Implication(claim_id=claim, direction="negative", basis="inferred_by_system",
                                               target_entity_id=english, target_label="Federal Reserve"))
        moved = self.store.merge_entities(fed, english, origin="model", reason="mismo banco")
        self.assertEqual(moved, 3)
        self.assertEqual(self.names(), ["Reserva Federal"])
        merged = self.store.get_entity(fed)
        self.assertEqual(merged["aliases"], ["FOMC", "Fed", "Federal Reserve"])
        self.assertEqual(merged["external_ids"], {"pais": "US", "fred": "FEDFUNDS"})
        self.assertEqual(len(self.store.search_claims(entity_id=fed)), 5)
        self.assertEqual(self.store.implications_for(claim)[0]["target_entity_id"], fed)
        # Las próximas extracciones ya reconocen el nombre absorbido.
        self.assertEqual(self.store.resolve_entity("federal reserve"), fed)
        log = self.store.db.execute("SELECT from_name, claims_moved, origin FROM entity_merges").fetchone()
        self.assertEqual(tuple(log), ("Federal Reserve", 3, "model"))

    def test_merge_can_be_undone(self):
        fed = self.entity("central_bank", "Federal Reserve", claims=3, fred="FEDFUNDS")
        spanish = self.entity("central_bank", "Reserva Federal", claims=2, aliases=["Fed"], pais="US")
        claim, _ = self.store.add_claim(Claim(source_id=self.src, statement="Otra", type="fact", status="verified"))
        self.store.add_implication(Implication(claim_id=claim, direction="negative", basis="inferred_by_system",
                                               target_entity_id=spanish, target_label="Reserva Federal"))
        self.store.merge_entities(fed, spanish, origin="model", reason="mismo banco")
        merge = self.store.entity_merges()[0]
        self.assertEqual((merge["from_name"], merge["into_name"], merge["claims_moved"], merge["undoable"]),
                         ("Reserva Federal", "Federal Reserve", 2, 1))
        self.assertEqual(self.store.undo_merge(merge["id"]), 2)
        self.assertEqual(self.names(), ["Federal Reserve", "Reserva Federal"])
        restored = self.store.resolve_entity("Reserva Federal")
        self.assertEqual(self.store.get_entity(restored)["aliases"], ["Fed"])
        self.assertEqual(self.store.get_entity(restored)["external_ids"], {"pais": "US"})
        self.assertEqual((self.store.get_entity(fed)["aliases"], self.store.get_entity(fed)["external_ids"]),
                         ([], {"fred": "FEDFUNDS"}))
        self.assertEqual((len(self.store.search_claims(entity_id=restored)), len(self.store.search_claims(entity_id=fed))),
                         (2, 3))
        self.assertEqual(self.store.implications_for(claim)[0]["target_entity_id"], restored)
        self.assertEqual(self.store.entity_merges(), [])
        # Deshecha a propósito: no vuelve a proponerse.
        self.assertEqual(self.store.merge_proposals(), [])
        self.assertFalse(self.store.add_merge_proposal(fed, restored, "model"))
        with self.assertRaises(KeyError):
            self.store.undo_merge(999)

    def test_old_merges_without_detail_cannot_be_undone(self):
        a = self.entity("company", "Uno")
        self.store.db.execute("INSERT INTO entity_merges (into_entity_id, from_name, from_type, claims_moved, created_at)"
                              " VALUES (?, 'Viejo', 'company', 1, 'antes')", (a,))
        merge = self.store.entity_merges()[0]
        self.assertEqual(merge["undoable"], 0)
        with self.assertRaisesRegex(ValidationError, "no se puede deshacer"):
            self.store.undo_merge(merge["id"])

    def test_merge_rejects_nonsense(self):
        a = self.entity("company", "Uno")
        with self.assertRaises(ValidationError):
            self.store.merge_entities(a, a)
        with self.assertRaises(KeyError):
            self.store.merge_entities(a, 999)

    def test_backup_copies_the_database(self):
        self.assertIsNone(self.store.backup("x"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "k.db")
            with Store(path) as store:
                store.add_source("youtube", "bbbbbbbbbb2")
                copy = store.backup("antes-de-fusionar")
            self.assertEqual(copy, path + ".antes-de-fusionar.bak")
            with Store(copy) as restored:
                self.assertEqual(restored.stats()["sources"], 1)


class ProposalTests(CatalogBase):
    def test_rules_find_mechanical_duplicates_only_within_a_type(self):
        marvell = self.entity("company", "Marvell Technology", claims=5)
        short = self.entity("company", "Marvell", claims=1)
        self.entity("company", "Aseguradora", claims=2)
        plural = self.entity("company", "Aseguradoras")
        asts = self.entity("company", "AST SpaceMobile", claims=9, ticker="ASTS")
        ticker = self.entity("security", "ASTS stock", claims=1, ticker="asts")
        self.entity("company", "Amazon", claims=4)
        self.entity("technology", "Amazon Web Services", claims=2)   # empresa y producto: no
        self.entity("technology", "Marvell")                          # otro tipo: no
        self.entity("sector", "Bolsa")
        self.entity("index", "Bolsa española")                        # general y particular: no
        found = catalog.propose_merges(self.store)
        self.assertEqual(found, {"rule": 3, "model": 0})
        proposals = {(p["from_name"], p["into_name"]): p for p in self.store.merge_proposals()}
        self.assertEqual(set(proposals), {("Marvell", "Marvell Technology"), ("Aseguradoras", "Aseguradora"),
                                          ("ASTS stock", "AST SpaceMobile")})   # se queda la más usada
        self.assertIn("ticker", proposals[("ASTS stock", "AST SpaceMobile")]["reason"])
        self.assertEqual(proposals[("Marvell", "Marvell Technology")]["into_claims"], 5)
        self.assertEqual(catalog.propose_merges(self.store), {"rule": 0, "model": 0})  # no se repiten
        self.assertEqual((marvell, short, plural, asts, ticker), (marvell, short, plural, asts, ticker))

    def test_model_proposals_are_validated(self):
        fed = self.entity("central_bank", "Reserva Federal", claims=3)
        english = self.entity("central_bank", "Federal Reserve", claims=7)
        usa = self.entity("country", "Estados Unidos", claims=4)
        us = self.entity("country", "United States", claims=1)
        tesla = self.entity("company", "Tesla", claims=2)
        seen = []

        def model(system, user):
            seen.append((system, user))
            if "Tesla" in user:
                return "no es JSON"
            return json.dumps({"groups": [
                {"ids": [fed, english], "reason": "mismo banco central en dos idiomas"},
                {"ids": [usa, us, 9999, "x", True]},          # ids inventados o mal formados: fuera
                {"ids": [fed, usa]},                            # un id ya usado no se reutiliza
                {"ids": [tesla]}, {"ids": "todos"}, "basura"]})

        lines = []
        found = catalog.propose_merges(self.store, model, lines.append)
        self.assertEqual(found, {"rule": 0, "model": 2})
        got = {(p["from_name"], p["into_name"], p["origin"]) for p in self.store.merge_proposals()}
        self.assertEqual(got, {("Reserva Federal", "Federal Reserve", "model"),
                               ("United States", "Estados Unidos", "model")})
        system, user = seen[-1]
        self.assertIn("exactamente la misma cosa", system)
        self.assertIn("ignóralas", system)
        self.assertIn('"name": "Reserva Federal"', user)
        self.assertTrue(lines)

    def test_apply_follows_chains_and_dismissed_are_not_proposed_again(self):
        a = self.entity("commodity", "Petróleo", claims=5)
        b = self.entity("commodity", "Oil", claims=3)
        c = self.entity("commodity", "Crude oil", claims=1)
        d = self.entity("commodity", "Oro", claims=2)
        e = self.entity("commodity", "Gold", claims=1)
        self.store.add_merge_proposal(a, b, "model", "petróleo")
        self.store.add_merge_proposal(b, c, "model", "crudo")       # apunta a una que va a desaparecer
        self.store.add_merge_proposal(d, e, "model", "oro")
        ids = [p["id"] for p in self.store.merge_proposals()]
        gold = next(p["id"] for p in self.store.merge_proposals() if p["from_name"] == "Gold")
        self.assertEqual(self.store.dismiss_merge_proposals([gold]), 1)
        done = catalog.apply_merges(self.store, ids)
        self.assertEqual(done, {"entidades": 2, "afirmaciones": 4})
        self.assertEqual(self.names(), ["Gold", "Oro", "Petróleo"])
        self.assertEqual(self.store.get_entity(a)["aliases"], ["Crude oil", "Oil"])
        self.assertEqual(self.store.merge_proposals(), [])
        self.assertFalse(self.store.add_merge_proposal(e, d, "rule"))  # descartada: no vuelve en ningún sentido


class DuplicateClaimTests(CatalogBase):
    def claim(self, statement, quote, **changes):
        base = dict(source_id=self.src, statement=statement, type="fact", status="verified", quote=quote)
        base.update(changes)
        return self.store.add_claim(Claim(**base))[0]

    def test_same_quote_in_same_video_keeps_the_richest_claim(self):
        plain = self.claim("AST obtuvo licencia para 248 satélites.", "licencia para 248 satélites en abril")
        rich = self.claim("En abril se otorgó a AST la licencia de 248 satélites.", "Licencia para 248 satélites, en abril.",
                          mechanism=[{"text": "La FCC la concede.", "basis": "inferred_by_system"}])
        other = self.claim("Otra idea distinta.", "una cita completamente distinta de la anterior")
        number = self.claim("La misma cita con otra cifra.", "licencia para 248 satélites en abril", metric_value=248.0)
        elsewhere, _ = self.store.add_source("youtube", "bbbbbbbbbb2")
        far = self.store.add_claim(Claim(source_id=elsewhere, statement="En otro vídeo.", type="fact",
                                         status="verified", quote="licencia para 248 satélites en abril"))[0]
        self.store.add_relation(other, "supports", plain)
        self.store.add_relation(plain, "refines", rich)   # quedaría como relación consigo misma: se elimina
        self.assertEqual(catalog.duplicate_claims(self.store), [(rich, plain)])
        self.assertEqual(catalog.remove_duplicate_claims(self.store), 1)
        row = self.store.claims_for_source(self.src, status="rejected")[0]
        self.assertEqual((row["id"], row["attrs"]["duplicado_de"]), (plain, rich))
        self.assertEqual([(r["relation"], r["other_id"]) for r in self.store.relations_for(rich)], [("supports", other)])
        self.assertEqual(self.store.relations_for(plain), [])
        self.assertEqual(len(self.store.search_claims()), 4)
        self.assertEqual((number, far), (number, far))
        self.assertEqual(catalog.remove_duplicate_claims(self.store), 0)  # repetir no cambia nada



class UnitTests(CatalogBase):
    def test_metrics_are_normalized_when_stored(self):
        from rezme.units import normalize
        cases = {
            (45, "miles de millones USD"): (45e9, "USD"), (7, "billion USD"): (7e9, "USD"),
            (2, "billones USD"): (2e12, "USD"), (9, "millones EUR"): (9e6, "EUR"),
            (17, "GW"): (17e9, "W"), (5, "mW"): (0.005, "W"), (4, "gigavatios"): (4e9, "W"),
            (328, "%"): (328, "%"), (3, "veces"): (3, "x"), (25, "puntos básicos"): (25, "pb"),
            (5, "millones de personas"): (5e6, "personas"), (4, "USD por acción"): (4, "USD/acción"),
            (6, "$/MWh"): (6, "USD/MWh"), (12, ""): (12, ""), (12, None): (12, ""),
            (3, "satélites"): (3, "satélites"), (2, "millones de yuanes"): (2e6, "CNY"),
        }
        for (value, unit), expected in cases.items():
            got = normalize(value, unit)
            self.assertAlmostEqual(got[0], expected[0], msg=unit)
            self.assertEqual(got[1], expected[1], unit)
        # Lo ambiguo no se adivina.
        for unit in ("trillones USD", "billones USD (trillones US)"):
            self.assertEqual(normalize(2, unit), (None, None), unit)
        self.assertEqual(normalize(None, "USD"), (None, None))

        self.store.add_claim(Claim(source_id=self.src, statement="Gasto de 45 mil millones.", type="statistic",
                                   status="verified", metric_value=45, metric_unit="miles de millones USD"))
        self.store.add_claim(Claim(source_id=self.src, statement="Sin cifra.", type="fact", status="verified"))
        rows = {r["statement"]: r for r in self.store.search_claims()}
        self.assertEqual((rows["Gasto de 45 mil millones."]["metric_value_abs"],
                          rows["Gasto de 45 mil millones."]["metric_unit_base"]), (45e9, "USD"))
        self.assertEqual(rows["Gasto de 45 mil millones."]["metric_value"], 45)   # lo dicho se conserva
        self.assertIsNone(rows["Sin cifra."]["metric_value_abs"])

    def test_v5_database_is_backfilled(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "k.db")
            with Store(path) as store:
                src, _ = store.add_source("youtube", "aaaaaaaaaa1")
                store.add_claim(Claim(source_id=src, statement="x", type="statistic", metric_value=3,
                                      metric_unit="millones USD"))
                a = store.upsert_entity("company", "A")
                b = store.upsert_entity("company", "B")
                store.merge_entities(a, b)
            db = sqlite3.connect(path)   # se devuelve la base a la forma v5
            db.executescript("ALTER TABLE claims DROP COLUMN metric_value_abs; ALTER TABLE claims DROP COLUMN "
                             "metric_unit_base; ALTER TABLE entity_merges DROP COLUMN moved_json; PRAGMA user_version = 5;")
            db.close()
            with Store(path) as store:
                claim = store.claims_for_source(1)[0]
                self.assertEqual((claim["metric_value_abs"], claim["metric_unit_base"]), (3e6, "USD"))
                self.assertEqual(store.entity_merges()[0]["undoable"], 0)


if __name__ == "__main__":
    unittest.main()
