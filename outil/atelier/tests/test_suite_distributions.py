from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.services.distributions import scrutiny_distribution

class DistributionTests(AtelierTestCase):
    def test_counts_have_denominator_no_fake_nominative_or_seats(self):
        value=scrutiny_distribution(self.make_scrutiny())
        self.assertEqual(value['proportion_base'],{'category':'voters','value':3})
        self.assertEqual(value['observed_counts'],{});self.assertFalse(value['nominative_list_reconciled'])
        self.assertIsNone(value['seat_coordinates']);self.assertFalse(value['publication_authorized'])
    def test_unknown_and_contradictory_counts_refused(self):
        s=self.make_scrutiny();s.count_for=None
        with self.assertRaises(ValidationError):scrutiny_distribution(s)
        s.count_for=2
        with self.assertRaises(ValidationError):scrutiny_distribution(s)
