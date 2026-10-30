// Vérification indépendante Node/OpenSSL du vecteur public fictif, hors WordPress.
import fs from 'node:fs';
import crypto from 'node:crypto';
const vector = JSON.parse(fs.readFileSync(new URL('../contracts/site-ed25519-vector.synthetic.json', import.meta.url), 'utf8'));
const sort = v => Array.isArray(v) ? v.map(sort) : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sort(v[k])])) : v;
const {review, sequence, ...record} = vector.record;
const canonical = JSON.stringify(sort(record)).replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
const digest = crypto.createHash('sha256').update(canonical).digest('hex');
const raw = Buffer.from(vector.public_keys[review.key_id], 'base64');
const key = crypto.createPublicKey({key:Buffer.concat([Buffer.from('302a300506032b6570032100','hex'), raw]), format:'der', type:'spki'});
if (digest !== review.snapshot_sha256 || !crypto.verify(null, Buffer.from(digest, 'ascii'), key, Buffer.from(review.signature, 'base64'))) throw Error('site_vector_verification_failed');
if (crypto.verify(null, Buffer.from('0'.repeat(64), 'ascii'), key, Buffer.from(review.signature, 'base64'))) throw Error('altered_digest_accepted');
console.log('node_openssl_site_vector_valid=true altered_digest_rejected=true wordpress_executed=false');
