"""Projection documentaire séparée des graines non qualifiées.

Le dossier de qualification est explicite, assertion par assertion. Aucun flag
des brouillons 0.1 ne suffit ; les textes primaires consultés sont liés par SHA.
"""
import hashlib
import json
from pathlib import Path
from .contract_v02 import validate


def export_qualified(dossier, source_root):
    source_root = Path(source_root).resolve()
    validate(dossier)
    available = {}
    for receipt_path in source_root.glob('*.receipt.json'):
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        text_path = receipt_path.with_name(receipt_path.name.replace('.receipt.json', '.txt'))
        if receipt.get('http_status') != 200 or not text_path.is_file():
            continue
        # Receipt hashes the UTF-8 visible text with normalized newlines.
        actual = hashlib.sha256(text_path.read_text(encoding='utf-8').encode('utf-8')).hexdigest()
        if actual == receipt.get('text_sha256'):
            available[(receipt['final_url'], actual)] = receipt
    for entry in dossier['entries']:
        for source in entry['sources']:
            receipt = available.get((source['url'], source['text_sha256']))
            if not receipt or receipt['retrieved_at'][:10] != source['consulted_on'] or receipt['publisher'] != source['publisher']:
                raise ValueError('consultation_primaire_non_prouvee')
    return json.loads(json.dumps(dossier, ensure_ascii=False))
