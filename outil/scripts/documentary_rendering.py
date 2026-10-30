"""Deterministic short notices from typed documentary facts; no political inference."""
import hashlib,json
LABELS={'institution':'institution','group':'groupe parlementaire référencé',
        'party':'parti ou groupement politique référencé','non_attachment':'catégorie de non-rattachement',
        'non_membership':'catégorie de non-inscription','administrative':'réunion administrative',
        'unqualified':'organisation de nature non qualifiée'}
def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def notice_id(kind,identifier):return 'NOTICE:'+kind+':'+hashlib.sha256(identifier.encode()).hexdigest()[:32]
def render_notice(kind,facts,version=2):
    if version == 3:
        return render_notice_v3(kind, facts)
    if version != 2:
        raise ValueError('notice_version_invalid')
    subject=facts[0]['data']
    if kind=='organization':
        label=LABELS[subject['type']]
        return (f"« {subject['name']} » figure dans les sources institutionnelles comme {label}. "
                f"Sa période source va de {subject.get('valid_from') or 'date de début non renseignée'} à {subject.get('valid_to') or 'date de fin non renseignée'}. "
                "Cette qualification ne décrit ni une orientation politique ni un statut actuel ; elle ne prouve aucune adhésion individuelle.")
    if kind!='person':raise ValueError('notice_subject_type_invalid')
    result=f"Le corpus institutionnel identifie cette personne sous le nom « {subject['display_name']} ». "
    if len(facts)>1:
        mandate=facts[1]['data'];institution=facts[2]['data']
        result+=(f"Un mandat est documenté au sein de {institution['name']}, du {mandate.get('starts_on') or 'début non renseigné'} "
                 f"au {mandate.get('ends_on') or 'terme non renseigné'}")
        if mandate.get('current_attested_on'):result+=f", avec une attestation de la source au {mandate['current_attested_on']}"
        result+='. '
    else:result+='Les sources reçues ne permettent pas de résumer un mandat daté. '
    return result+"Les mandats et relations figurent séparément dans l’historique ; aucune situation actuelle ou adhésion partisane n’est déduite des dates manquantes."


def render_notice_v3(kind, facts):
    subject = facts[0]['data']
    if kind == 'organization':
        result = f"{subject['name']} : {LABELS[subject['type']]}. "
        result += f"Période documentée : {subject.get('valid_from') or 'début inconnu'} — {subject.get('valid_to') or 'fin inconnue'}. "
        return result + 'Cette catégorie ne prouve ni une adhésion individuelle ni un statut actuel.'
    if kind != 'person':
        raise ValueError('notice_subject_type_invalid')
    if len(facts) == 1:
        return f"{subject['display_name']} figure dans le corpus institutionnel. Aucun mandat daté ne peut être résumé avec les sources reçues."
    mandate, institution = facts[1]['data'], facts[2]['data']
    result = f"{subject['display_name']} — {mandate.get('title') or 'mandat parlementaire documenté'}, {institution['name']}. "
    result += f"Période documentée : {mandate.get('starts_on') or 'début inconnu'} — {mandate.get('ends_on') or 'fin inconnue'}. "
    if mandate.get('current_attested_on'):
        result += f"Mandat attesté au {mandate['current_attested_on']}. "
    return result + 'Une date manquante ne permet pas de conclure à une fonction actuelle ; les rattachements ne prouvent pas une adhésion partisane.'
