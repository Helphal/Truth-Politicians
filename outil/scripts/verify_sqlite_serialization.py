"""Distinct-process SQLite reservation / interrupted correction / stale review test."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(sys.argv[0]).resolve().parent.parent
sys.path.insert(0,str(ROOT))

def setup():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE','dv_project.settings')
    import django
    django.setup()

def child(mode,dossier_id,user_id):
    setup()
    from django.conf import settings
    from django.db import connections,transaction,OperationalError
    from django.core.exceptions import ValidationError
    from django.contrib.auth import get_user_model
    from atelier.models import EditorialDossier
    from atelier.services.publication import record_human_review
    settings.DATABASES['default']['OPTIONS']['timeout']=0.25
    connections.close_all()
    dossier=EditorialDossier.objects.get(pk=dossier_id)
    if mode=='hold':
        with transaction.atomic():
            dossier.current_revision=2;dossier.save(update_fields=['current_revision'])
            print('LOCKED',flush=True)
            if sys.stdin.readline().strip()!='commit':raise SystemExit(2)
        print('COMMITTED',flush=True)
    else:
        revision=dossier.revisions.get(revision=1)
        try:
            record_human_review(dossier_id=dossier.pk,expected_revision=1,expected_digest=revision.snapshot_digest,
                user=get_user_model().objects.get(pk=user_id),decision='approve',environment='test')
            print('REVIEWED')
        except OperationalError as exc:
            if 'locked' not in str(exc):raise
            print('LOCK_REFUSED')
        except ValidationError:print('STALE_REFUSED')

def main():
    started=time.monotonic();attempts=[]
    with tempfile.TemporaryDirectory(prefix='dv-two-process-') as temporary:
        os.environ.update(DV_DATABASE_PATH=str(Path(temporary)/'test.sqlite3'),DV_RUNTIME_DIR=str(Path(temporary)/'runtime'),DV_ENV='test',DV_NETWORK_MODE='disabled')
        setup()
        from django.core.management import call_command
        from django.db import connections
        from atelier.tests.support import AtelierTestCase
        from atelier.models import EditorialDossier,HumanReview
        call_command('migrate',interactive=False,verbosity=0)
        fixture=AtelierTestCase();fixture.setUp();d,r,_=fixture.make_revision('speech_vote');u=fixture.make_user()
        # Authority is explicitly bound to this disposable SYNTH reviewer in
        # each child process; production rules and real identities are unchanged.
        os.environ['DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID']=str(u.dv_profile.internal_uid)
        args=[sys.executable,str(ROOT/'scripts/verify_sqlite_serialization.py')]
        def holder():
            process=subprocess.Popen([*args,'hold',str(d.pk),str(u.pk)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
            first_line=process.stdout.readline().strip()
            if first_line!='LOCKED':
                process.terminate();out,err=process.communicate(timeout=10)
                raise AssertionError('holder_start_failed:'+first_line+out+err)
            return process
        def review():
            attempt_started=time.monotonic()
            p=subprocess.run([*args,'review',str(d.pk),str(u.pk)],capture_output=True,text=True,encoding='utf-8',timeout=20)
            assert p.returncode==0,p.stderr
            attempts.append({'outcome':p.stdout.strip(),'elapsed_seconds':round(time.monotonic()-attempt_started,4)})
            return p.stdout.strip()
        connections.close_all();first=holder()
        try:
            assert review()=='LOCK_REFUSED'
            first.terminate();first.wait(timeout=10)
            d.refresh_from_db();assert d.current_revision==1 and HumanReview.objects.count()==0
            assert review()=='REVIEWED'
            connections.close_all();second=holder()
            try:
                assert review()=='LOCK_REFUSED'
                second.stdin.write('commit\n');second.stdin.flush();out,err=second.communicate(timeout=10)
                assert second.returncode==0,err
                assert review()=='STALE_REFUSED'
            finally:
                if second.poll() is None:second.terminate();second.wait(timeout=10)
            assert HumanReview.objects.count()==1
            result={'distinct_processes':True,'sqlite_transaction_mode':'IMMEDIATE','concurrent_writer_refused':True,
                'interrupted_correction_rolled_back':True,'retry_after_interruption_succeeded':True,'stale_review_after_commit_refused':True,
                'reviews':1,'scope':'disposable synthetic SQLite only','multi_writer_claim':False,
                'attempts':attempts,'elapsed_seconds':round(time.monotonic()-started,4),'sqlite_lock_timeout_seconds':0.25}
            print(json.dumps(result))
        finally:
            if first.poll() is None:first.terminate();first.wait(timeout=10)
            connections.close_all()

if __name__=='__main__':
    if len(sys.argv)>1:child(sys.argv[1],int(sys.argv[2]),int(sys.argv[3]))
    else:main()
