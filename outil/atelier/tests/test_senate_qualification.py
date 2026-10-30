from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError

from atelier.services.senate_qualification import SOURCE_IDS, verify_chain_contents

from .support import AtelierTestCase


def fixture_contents():
    values = {source_id: b"document officiel" for source_id in SOURCE_IDS}
    values["SENAT_TEXT_PPL25_298"] = (
        "auprès d'une région d'outre-mer La production maximale d'hydrocarbures"
    ).encode()
    values["SENAT_AMENDMENTS_ADOPTED_298"] = b" ".join(
        f'/298/Amdt_{number}.html'.encode() for number in (13, 4, 9, 12)
    )
    values["SENAT_AMENDMENT_298_13"] = (
        "Adopté ARTICLE 1er insérer les mots de la collectivité territoriale de Saint-Pierre-et-Miquelon"
    ).encode()
    values["SENAT_SESSION_20260129_013"] = (
        "L'article 2 est supprimé. Je mets aux voix, dans le texte de la commission, modifié. "
        "Résultat du scrutin n° 178."
    ).encode()
    values["SENAT_TEXT_ADOPTED_050"] = (
        "collectivité territoriale de Saint-Pierre-et-Miquelon "
        "2° Le II de l'article 25 est abrogé. Article 2 (Supprimé)"
    ).encode()
    values["SENAT_SCRUTIN_2025_178"] = b"scrutiny-fixture"
    return values


class SenateQualificationTests(AtelierTestCase):
    def test_exact_chain_requires_complete_adopted_amendment_set(self):
        contents = fixture_contents()
        with self.assertRaisesMessage(ValidationError, "senate_adopted_amendment_set_mismatch"):
            changed = deepcopy(contents)
            changed["SENAT_AMENDMENTS_ADOPTED_298"] = changed[
                "SENAT_AMENDMENTS_ADOPTED_298"
            ].replace(b"/298/Amdt_13.html", b"")
            verify_chain_contents(changed)

    def test_exact_chain_requires_session_order(self):
        contents = fixture_contents()
        contents["SENAT_SESSION_20260129_013"] = (
            "Résultat du scrutin n° 178. L'article 2 est supprimé. "
            "Je mets aux voix, dans le texte de la commission, modifié."
        ).encode()
        with self.assertRaisesMessage(ValidationError, "senate_session_sequence_unexpected"):
            verify_chain_contents(contents)

    def test_exact_chain_rejects_negated_or_changed_operative_clause(self):
        contents = fixture_contents()
        contents["SENAT_TEXT_ADOPTED_050"] = contents["SENAT_TEXT_ADOPTED_050"].replace(
            "est abrogé".encode(), "n'est pas abrogé".encode()
        )
        with self.assertRaisesMessage(ValidationError, "senate_adopted_text_050_unexpected"):
            verify_chain_contents(contents)

        contents = fixture_contents()
        contents["SENAT_TEXT_ADOPTED_050"] = contents["SENAT_TEXT_ADOPTED_050"].replace(
            "Saint-Pierre-et-Miquelon".encode(), "une autre collectivité".encode()
        )
        with self.assertRaisesMessage(ValidationError, "senate_adopted_text_050_unexpected"):
            verify_chain_contents(contents)

    @patch("atelier.services.senate_qualification.parse_scrutiny_html")
    def test_exact_chain_accepts_verified_official_sequence(self, parser):
        parser.return_value = {
            "source_identifier": "178",
            "voters": 340,
            "expressed": 332,
            "count_for": 227,
            "count_against": 105,
            "count_abstention": 8,
        }
        result = verify_chain_contents(fixture_contents())
        self.assertEqual(result["resulting_text"], "50")
        self.assertEqual(result["counts"]["count_for"], 227)
