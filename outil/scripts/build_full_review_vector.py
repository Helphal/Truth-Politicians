"""Generate only an ephemeral, conspicuously synthetic Ed25519 interoperability vector."""
import base64,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
os.environ.setdefault('DJANGO_SETTINGS_MODULE','dv_project.settings')
sys.path.insert(0,str(ROOT))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from atelier.services.canonical import canonical_bytes,sha256_digest
from atelier.services.full_review import FORMAT,approval_bytes,verify_detached_review
path=ROOT/'contracts/dv-full-review-test-vector.json'
if path.exists():raise SystemExit('vector_exists_no_overwrite')
public={'title':'FIXTURE UNIQUEMENT — contenu intégral','evidence':[{'exact_quote':'Citation fictive.'}],
    'counterevidence':[{'statement':'Contre-élément fictif.'}],'catalogue':{'persons':[{'id':'TEST-PERSON','display_name':'Personne fictive'}]}}
bundle={'format':FORMAT,'record_id':'TEST-FULL-REVIEW','record_revision':1,'environment':'test','destination':'dv-site:test','content_kind':'speech_vote','data_scope':'synthetic',
    'snapshot_digest':sha256_digest(public),'public':public,'source_versions':[{'source_id':'TEST-SOURCE','version_id':'TEST-1','sha256':'a'*64,'public_url':'https://example.org/fixture'}],'method_version':'TEST-NOT-POLITICAL'}
statement={'algorithm':'ed25519','key_id':'test-full-review-fixture','bundle_sha256':sha256_digest(bundle),'record_id':bundle['record_id'],'record_revision':1,'decision':'approve','reviewed_at':'2026-09-13T22:00:00Z'}
key=Ed25519PrivateKey.generate();message=approval_bytes(bundle,statement)
proof={'statement':statement,'signature':base64.b64encode(key.sign(message)).decode()}
public_key=base64.b64encode(key.public_key().public_bytes_raw()).decode()
result=verify_detached_review(bundle,proof,trusted_public_keys={statement['key_id']:public_key},environment='test',destination='dv-site:test',allow_test_fixtures=True)
path.write_text(json.dumps({'classification':'synthetic_test_only_never_human_review','bundle':bundle,'proof':proof,'public_key_base64':public_key,'message_utf8':message.decode(),'python_verification':result},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'vector_file':path.name,'private_key_persisted':False,'verified':result['verified_detached_signature']}))
