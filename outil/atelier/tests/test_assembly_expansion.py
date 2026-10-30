import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile
from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.models import Person, VoteObservation, Affiliation, AffiliationObservation
from atelier.services.assembly_expansion import expand_materialized_votes
from atelier.services.parsers.assembly import normalize_scrutiny


class AssemblyExpansionTests(AtelierTestCase):
    def test_congress_number_one_never_overwrites_assembly_number_one(self):
        common = {"numero":"1", "legislature":"16", "dateScrutin":"2024-03-04", "titre":"Fictif"}
        an = normalize_scrutiny({**common, "uid":"VTANR5L16V1"})
        congress = normalize_scrutiny({**common, "uid":"VTCGR5L16V1"})
        self.assertNotEqual(an["institution"], congress["institution"])

    def test_reimport_same_bytes_no_duplication_no_invented_mandate_or_name(self):
        doc = self.make_document()
        scrutiny = self.make_scrutiny(doc)
        raw = {"numero":scrutiny.source_identifier, "legislature":scrutiny.legislature,
            "dateScrutin":scrutiny.occurred_on.isoformat(), "titre":scrutiny.official_title,
            "uid":"VTAN-SYNTHETIC", "ventilationVotes":{"organe":{"groupes":{"groupe":{
                "organeRef":"SYNTHETIC-GROUP", "vote":{"decompteNominatif":{"pours":{"votant":{
                    "acteurRef":"SYNTHETIC-PERSON", "mandatRef":"UNVERIFIED-MANDATE"}}}}}}}}}
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/"synthetic.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("synthetic.json", json.dumps(raw))
            doc.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            doc.save()
            first = expand_materialized_votes(doc, path)
            second = expand_materialized_votes(doc, path)
            self.assertEqual(first["observations_added"], 1)
            self.assertEqual(second["observations_added"], 0)
            self.assertEqual(Person.objects.get().identity_status, "unresolved")
            self.assertIsNone(VoteObservation.objects.get().mandate_id)
            self.assertFalse(Affiliation.objects.exists())
            observation = AffiliationObservation.objects.get()
            self.assertEqual(observation.observed_on, scrutiny.occurred_on)
            self.assertEqual(observation.known_at, doc.captured_at)
            self.assertNotIn('starts_on', observation.payload)
            self.assertNotIn('ends_on', observation.payload)
            doc.sha256 = "0"*64
            with self.assertRaisesMessage(ValidationError, "assembly_archive_not_pinned"):
                expand_materialized_votes(doc, path)
