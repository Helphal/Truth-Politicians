import {readFileSync} from 'node:fs';
import {createHash,createPublicKey,verify} from 'node:crypto';
const path=process.argv[2] || 'contracts/dv-full-review-test-vector.json';
const vector=JSON.parse(readFileSync(path,'utf8'));
function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value!==null&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
const hash=v=>createHash('sha256').update(canonical(v),'utf8').digest('hex');
const statement=vector.proof.statement;
const message='DV-FULL-REVIEW-ED25519-V1\n'+canonical(statement);
const raw=Buffer.from(vector.public_key_base64,'base64');
const key=createPublicKey({key:Buffer.concat([Buffer.from('302a300506032b6570032100','hex'),raw]),format:'der',type:'spki'});
const valid=vector.classification==='synthetic_test_only_never_human_review' && statement.key_id.startsWith('test-') &&
  statement.bundle_sha256===hash(vector.bundle) && vector.bundle.snapshot_digest===hash(vector.bundle.public) && message===vector.message_utf8 &&
  verify(null,Buffer.from(message,'utf8'),key,Buffer.from(vector.proof.signature,'base64'));
const tampered=structuredClone(vector.bundle);tampered.public.counterevidence[0].statement+=' ALTERED';
if(!valid || statement.bundle_sha256===hash(tampered))throw new Error('full_review_vector_failed');
console.log(JSON.stringify({scope:'independent_node_ed25519_fixture_only',signature_valid:true,full_bundle_digest_valid:true,counterevidence_tamper_rejected:true,wordpress_php_executed:false,publication_authorized:false}));
