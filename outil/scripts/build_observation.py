"""Read-only, transaction-consistent build observation on an explicitly selected database."""
import hashlib, sqlite3
from datetime import datetime,timezone
from pathlib import Path

COUNTS={'people':'atelier_person','mandates':'atelier_mandate','affiliations':'atelier_affiliation','votes':'atelier_voteobservation','scrutinies_materialized':'atelier_scrutiny','staged_scrutinies':'atelier_stagedscrutiny','indexed_passages':'atelier_indexedpassage','dossiers':'atelier_editorialdossier','human_reviews':'atelier_humanreview','users':'auth_user'}
COUNTS.update(research_work_files='atelier_inquirycase',indicator_contributions='atelier_indicatorcontribution',indicator_aggregates='atelier_indicatoraggregate',site_publication_confirmations='atelier_sitepublicationconfirmation')

def observe(database):
    database=Path(database).resolve()
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN')
        counts={key:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for key,table in COUNTS.items()}
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for key,table in {'research_preparations':'atelier_researchpreparation','retired_scrutinies':'atelier_scrutinyidentitycorrection','feed_exclusions':'atelier_feedexclusion'}.items():
            counts[key]=db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] if table in tables else 0
        counts['active_scrutinies']=counts['scrutinies_materialized']-counts['retired_scrutinies']
        counts['speech_vote_dossiers']=db.execute("SELECT COUNT(*) FROM atelier_editorialdossier WHERE content_kind='speech_vote'").fetchone()[0]
        counts['unresolved_people']=db.execute("SELECT COUNT(*) FROM atelier_person WHERE identity_status='unresolved'").fetchone()[0]
        counts['conflicting_people']=db.execute("SELECT COUNT(*) FROM atelier_person WHERE identity_status='conflict'").fetchone()[0]
        migrations=[x[0] for x in db.execute("SELECT name FROM django_migrations WHERE app='atelier' ORDER BY name")]
        reference_cursors=[{'document_id':x[0],'cursor':x[1],'total':x[2],'completed':bool(x[3])} for x in db.execute("SELECT document_id,cursor,total_members,completed FROM atelier_archivescan WHERE parser_version LIKE 'DV-AN-REFERENCE-%'")]
    return {'observed_at':datetime.now(timezone.utc).isoformat(),'observation_version':'DV-BUILD-OBSERVATION-1','counts':counts,'working_copy_migrations':migrations,'reference_cursors':reference_cursors,'publication_authorized':False,'private_installed':False,'site_connected':False}
