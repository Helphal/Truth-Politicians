"""Réception d'inventaire pédagogique existant en préparation, sans publication.

Le contrat dv.explain-public/0.1.0 n'est pas un lot RC005 dv-site-bridge.
Les graines ne constituent ni une qualification des sources ni une revue humaine.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def prepare(output):
    project=Path(__file__).resolve().parents[1]
    # Initialisation des modèles seulement ; aucune connexion ni requête SQL.
    os.environ['DJANGO_SETTINGS_MODULE']='dv_project.settings'
    os.environ['DV_DATABASE_PATH']=':memory:'
    sys.path.insert(0,str(project))
    import django
    django.setup()
    from atelier.services.explain import SEED
    rows=[{**entry,'version':1,'authorization_state':'draft','verified_on':None,
           'qualification':'source_review_and_receiver_contract_mapping_required'} for entry in SEED]
    payload={'format':'dv.explain-preparation/rec02-1','source_module':'atelier.services.explain.SEED',
             'source_module_sha256':hashlib.sha256((project/'atelier/services/explain/__init__.py').read_bytes()).hexdigest(),
             'authorization_state':'draft','partisan_commentary':False,'human_review_claimed':False,
             'site_import_admissible':False,'reason':'Les graines ne prouvent pas une qualification des sources ni la compatibilité avec le récepteur choisi.',
             'entries':rows,
             'source_checks':[{'scope':'role institutionnel uniquement; texte complet non qualifie',
                 'checked_on':None,'url':'https://www.assemblee-nationale.fr/dyn/role-et-pouvoir-de-assemblee'},
                 {'scope':'role institutionnel uniquement; texte complet non qualifie','checked_on':None,
                  'url':'https://www.senat.fr/connaitre-le-senat/role-et-fonctionnement.html'}]}
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:json.dump(payload,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'prepared':len(rows),'admissible_for_site_import':False,'database_writes':0,'human_review_claimed':False}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    prepare(parser.parse_args().output)
