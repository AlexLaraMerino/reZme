import json
import unittest

from rezme import Claim, Store, backends, crosscheck


class CrossBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.sources = {}
        for name, video, channel in (("leo1", "aaaaaaaaaa1", "LEO"), ("leo2", "bbbbbbbbbb2", "LEO"),
                                     ("eme", "cccccccccc3", "EME"), ("vic", "dddddddddd4", "VIC")):
            self.sources[name], _ = self.store.add_source("youtube", video, title=f"Vídeo {name}",
                                                          channel=channel.title(), channel_id=channel,
                                                          published_at="2026-09-01")
        self.asts = self.store.upsert_entity("company", "AST SpaceMobile")
        self.oklo = self.store.upsert_entity("company", "Oklo")

    def tearDown(self):
        self.store.close()

    def claim(self, source, statement, entity=None, **changes):
        base = dict(source_id=self.sources[source], statement=statement, type="fact", status="verified",
                    entity_id=self.asts if entity is None else entity)
        base.update(changes)
        return self.store.add_claim(Claim(**base))[0]


class CrossCheckTests(CrossBase):
    def test_only_entities_shared_by_independent_channels(self):
        self.claim("leo1", "AST tiene 10 satélites.")
        self.claim("leo2", "AST lanzará más.")              # mismo canal: no hace compartida a la entidad
        self.assertEqual(crosscheck.shared_entities(self.store), [])
        self.claim("eme", "AST no da banda ancha.")
        self.claim("leo1", "Oklo solo en un canal.", entity=self.oklo)
        shared = crosscheck.shared_entities(self.store)
        self.assertEqual([(e["name"], len(e["claims"])) for e in shared], [("AST SpaceMobile", 3)])

    def test_same_figure_alone_is_not_treated_as_support(self):
        a = self.claim("leo1", "El bono llegó al 5 % en 2007.", metric_value=5, metric_unit="%")
        b = self.claim("eme", "El bono toca hoy el 5 %.", metric_value=5, metric_unit="%")
        result = crosscheck.cross_check(self.store, lambda system, user: '{"relations": []}')
        self.assertEqual((result["supports"], result["revisadas"]), (0, 1))
        claims = {c["id"]: c for c in self.store.search_claims(limit=50)}
        self.assertEqual((claims[a]["supported_by"], claims[b]["supported_by"]), (0, 0))

    def test_model_relations_are_validated_and_counted_by_independent_channel(self):
        viable = self.claim("leo1", "AST puede dar banda ancha masiva.", type="opinion")
        again = self.claim("leo2", "AST dará banda ancha a todos.", type="forecast")
        no = self.claim("eme", "La física limita AST a un servicio de respaldo.", type="opinion")
        also_no = self.claim("vic", "AST no tendrá capacidad para banda ancha.", type="opinion")
        seen = []

        def model(system, user):
            seen.append((system, user))
            return json.dumps({"relations": [
                {"a": no, "b": viable, "relation": "contradicts", "reason": "capacidad"},
                {"a": also_no, "b": no, "relation": "supports", "reason": "coinciden"},
                {"a": viable, "b": again, "relation": "supports"},          # mismo canal: fuera
                {"a": no, "b": 9999, "relation": "supports"},                # id inventado: fuera
                {"a": no, "b": also_no, "relation": "destroys"},             # relación inventada: fuera
                {"a": no, "b": no, "relation": "supports"}, "basura"]})

        lines = []
        result = crosscheck.cross_check(self.store, model, progress=lines.append)
        self.assertEqual((result["contradicts"], result["supports"], result["revisadas"]), (1, 1, 1))
        system, user = seen[0]
        self.assertIn("canales distintos", system)
        self.assertIn("Entidad: AST SpaceMobile", user)
        self.assertIn('"canal": "A"', user)
        self.assertIn('"canal": "B"', user)
        claims = {c["id"]: c for c in self.store.search_claims(limit=50)}
        self.assertEqual((claims[no]["supported_by"], claims[no]["contradicted_by"]), (1, 1))
        self.assertEqual((claims[viable]["supported_by"], claims[viable]["contradicted_by"]), (0, 1))
        pairs = crosscheck.cross_relations(self.store, "contradicts")
        self.assertEqual((pairs[0]["a_channel"], pairs[0]["b_channel"], pairs[0]["reason"], pairs[0]["entity"]),
                         ("Eme", "Leo", "capacidad", "AST SpaceMobile"))
        info = crosscheck.summary(self.store)
        self.assertEqual((info["canales"], info["entidades"], info["pendientes"], info["contradicciones"], info["apoyos"]),
                         (3, 1, 0, 1, 1))

        # Ya contrastada: no se vuelve a llamar hasta que haya afirmaciones nuevas.
        crosscheck.cross_check(self.store, model)
        self.assertEqual(len(seen), 1)
        self.claim("vic", "Una afirmación nueva sobre AST.")
        self.assertEqual(crosscheck.summary(self.store)["pendientes"], 1)
        crosscheck.cross_check(self.store, model)
        self.assertEqual(len(seen), 2)

    def test_failures_are_retried_later_and_access_errors_stop(self):
        self.claim("leo1", "Uno.")
        self.claim("eme", "Dos.")

        def broken(system, user):
            raise RuntimeError("timed out")

        result = crosscheck.cross_check(self.store, broken)
        self.assertEqual((result["revisadas"], crosscheck.summary(self.store)["pendientes"]), (0, 1))

        def denied(system, user):
            raise backends.BackendUnavailable("La clave API no es válida.")

        with self.assertRaises(backends.BackendUnavailable):
            crosscheck.cross_check(self.store, denied)
        self.assertEqual(crosscheck.cross_check(self.store, lambda s, u: "no es JSON")["revisadas"], 1)

    def test_big_entities_are_split_keeping_two_channels_per_call(self):
        for i in range(200):
            self.claim("leo1", f"Afirmación de Leo número {i}.")
        for i in range(30):
            self.claim("eme", f"Afirmación de Emérito número {i}.")
        entity = crosscheck.shared_entities(self.store)[0]
        batches = crosscheck._batches(entity)
        self.assertEqual([len(b) for b in batches], [140, 120])
        for batch in batches:
            self.assertEqual(len({c["channel_id"] for c in batch}), 2)
        self.assertEqual(crosscheck.summary(self.store)["llamadas"], 2)


if __name__ == "__main__":
    unittest.main()
