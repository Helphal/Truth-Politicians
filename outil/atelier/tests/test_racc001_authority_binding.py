"""Contrôle _review_authority_binding_valid — comptes suspendus refusés."""
from types import SimpleNamespace

from django.test import override_settings

from atelier.models import Environment, UserProfile
from atelier.services.publication import _review_authority_binding_valid
from atelier.tests.support import AtelierTestCase


class ReviewAuthorityBindingUnitTests(AtelierTestCase):
    def test_suspended_profile_rejected_despite_uid_match(self):
        user = self.make_user()
        uid = str(user.dv_profile.internal_uid)
        review = SimpleNamespace(authority_internal_uid=uid)
        user.dv_profile.account_status = UserProfile.AccountStatus.SUSPENDED
        user.dv_profile.save()
        with override_settings(DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=uid):
            self.assertFalse(_review_authority_binding_valid(review, user.dv_profile, Environment.TEST))
