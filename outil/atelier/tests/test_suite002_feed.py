from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from django.test import override_settings
from django.core.exceptions import ValidationError
from atelier.tests.support import AtelierTestCase
from atelier.tests import test_suite_indicators as prior
from atelier.models import FeedExclusion
from atelier.services.confirmed_feed import confirmed_feed
from atelier.services.editorial_feed import accept_confirmation
from atelier.services.publication import authorize_and_queue,queue_suspension
from atelier.services.indicators import save_contribution

class ConfirmedFeedTests(AtelierTestCase):
    def setUp(self):
        super().setUp()
        self.feed_runtime=TemporaryDirectory(prefix='dv-feed-test-')
        self.addCleanup(self.feed_runtime.cleanup)
        setting=override_settings(RUNTIME_DIR=Path(self.feed_runtime.name));setting.enable();self.addCleanup(setting.disable)
    signed=prior.IndicatorTests.signed
    period=prior.EditorialFeedTests.period
    confirmation=prior.EditorialFeedTests.confirmation
    def test_empty_week_has_no_content_and_no_contacts(self):
        result=confirmed_feed(**self.period())
        self.assertEqual(result['items'],[]);self.assertFalse(result['sendable'])
        self.assertNotIn('subscribers',result);self.assertNotIn('consent',result)
    def test_observed_suspension_latches_and_old_revision_cannot_reappear(self):
        d,r,u,item,keys=self.signed()
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event=authorize_and_queue(r,'test');s,sig,sitekeys=self.confirmation(event)
            with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=sitekeys):
                accept_confirmation(event,s,sig);period=self.period()
                self.assertEqual(len(confirmed_feed(**period)['items']),1)
                d.suspended=True;d.save(update_fields=['suspended'])
                removed=confirmed_feed(**period);self.assertEqual(removed['items'],[])
                d.suspended=False;d.save(update_fields=['suspended'])
                self.assertEqual(confirmed_feed(**period)['items'],[])
                self.assertEqual(FeedExclusion.objects.count(),1)
                self.assertFalse(removed['exclusions'][0]['remote_withdrawal_confirmed'])
    def test_lost_confirmation_replay_and_restoration_fail_closed(self):
        d,r,u,item,keys=self.signed()
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event=authorize_and_queue(r,'test');s,sig,sitekeys=self.confirmation(event)
            with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=sitekeys):
                period=self.period();self.assertEqual(confirmed_feed(**period)['items'],[])
                accept_confirmation(event,s,sig);accept_confirmation(event,s,sig)
                self.assertEqual(len(confirmed_feed(**period)['items']),1)
                with TemporaryDirectory() as tmp,override_settings(RUNTIME_DIR=Path(tmp)):
                    (Path(tmp)/'.dv-restored-unreconciled').touch()
                    result=confirmed_feed(**period);self.assertEqual(result['items'],[]);self.assertTrue(result['reconciliation_required'])
                self.assertFalse(FeedExclusion.objects.exists())
    def test_superseded_revision_and_relative_date_refused(self):
        d,r,u,item,keys=self.signed()
        holder=str(u.dv_profile.internal_uid)
        with override_settings(DV_DETACHED_REVIEW_PUBLIC_KEYS=keys, DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID=holder):
            event=authorize_and_queue(r,'test');s,sig,sitekeys=self.confirmation(event)
            with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=sitekeys):
                broken=deepcopy(s);broken['published_at']='tomorrow'
                with self.assertRaises(ValidationError):accept_confirmation(event,broken,sig)
                broken['published_at']='2026-02-30T00:00:00Z'
                with self.assertRaises(ValidationError):accept_confirmation(event,broken,sig)
                accept_confirmation(event,s,sig)
                d.refresh_from_db();d.current_revision+=1;d.save(update_fields=['current_revision'])
                self.assertEqual(confirmed_feed(**self.period())['items'],[])
    def test_real_contribution_is_not_created_by_preparation(self):
        d,r,_=self.make_revision('speech_vote');d.data_scope='real';d.save(update_fields=['data_scope'])
        with self.assertRaisesMessage(ValidationError,'indicator_real_contribution_requires_adopted_review_workflow'):
            save_contribution(r,prior.entry(d.stable_id,sources=r.source_snapshot))
    def test_correction_needs_own_confirmation_and_replaces_old_revision(self):
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from atelier.services.canonical import canonical_bytes
        from atelier.services.editorial_feed import DOMAIN
        from atelier.services.revisions import create_revision,record_controls,deterministic_control_results
        d,r,_=self.make_revision('official_vote');first=authorize_and_queue(r,'test')
        statement,signature,keys=self.confirmation(first)
        with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=keys):
            accept_confirmation(first,statement,signature)
            self.assertEqual(len(confirmed_feed(**self.period())['items']),1)
            revised=create_revision(dossier=d,public_payload={**r.public_payload,'official_title':'Titre fictif corrigé'},
                source_snapshot=r.source_snapshot,title=r.title,short_answer=r.short_answer,main_caveat=r.main_caveat,reasoning=r.reasoning)
            record_controls(revised,deterministic_control_results(revised),environment='test')
            self.assertEqual(confirmed_feed(**self.period())['items'],[])
            second=authorize_and_queue(revised,'test');updated,_,_=self.confirmation(second)
            key=Ed25519PrivateKey.generate();updated['key_id']='test-correction';updated['change_kind']='correction'
            keys2={**keys,'test-correction':base64.b64encode(key.public_key().public_bytes_raw()).decode()}
            sig=base64.b64encode(key.sign(DOMAIN+canonical_bytes(updated))).decode()
            with override_settings(DV_SITE_PUBLICATION_PUBLIC_KEYS=keys2):
                accept_confirmation(second,updated,sig)
                result=confirmed_feed(**self.period())
                self.assertEqual([(x['published_revision'],x['change_kind']) for x in result['items']],[(2,'correction')])
