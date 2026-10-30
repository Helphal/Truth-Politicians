"""EXPLAIN-01 — module pédagogique sourcé et versionné (Outil uniquement)."""
from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from atelier.models import ExplainEntry


SEED: tuple[dict, ...] = (
    {
        "stable_id": "explain.assemblee.role",
        "institution": ExplainEntry.Institution.ASSEMBLEE,
        "role_or_procedure": "depute",
        "title": "Rôle d'un député",
        "short_text": (
            "Un député est élu pour représenter la Nation à l'Assemblée nationale. "
            "Il participe aux débats, aux commissions et aux votes selon les règles "
            "de la chambre."
        ),
        "detailed_text": (
            "Le député exerce un mandat parlementaire distinct d'une fonction "
            "gouvernementale. Ses votes individuels ne sont exploitables que lorsque "
            "le scrutin est public et nominatif. L'appartenance à un groupe "
            "parlementaire n'équivaut pas automatiquement à une position personnelle "
            "prouvée sur chaque texte."
        ),
        "official_sources": [
            {
                "label": "Assemblée nationale — rôle du député",
                "url": "https://www.assemblee-nationale.fr/dyn/role-et-pouvoirs-du-depute",
            }
        ],
        "limits_and_variants": "Les règles de publicité des scrutins varient selon la procédure.",
        "dependencies": [],
    },
    {
        "stable_id": "explain.senat.role",
        "institution": ExplainEntry.Institution.SENAT,
        "role_or_procedure": "senateur",
        "title": "Rôle d'un sénateur",
        "short_text": (
            "Un sénateur représente les collectivités territoriales au Sénat et "
            "participe à l'élaboration de la loi dans le cadre du bicamérisme."
        ),
        "detailed_text": (
            "Comme à l'Assemblée, un vote individuel n'est disponible que pour les "
            "scrutins publics nominatifs publiés. L'absence de détail nominatif doit "
            "rester inconnue ou non concluante, jamais inventée."
        ),
        "official_sources": [
            {
                "label": "Sénat — présentation",
                "url": "https://www.senat.fr/role/fiche/senateur.html",
            }
        ],
        "limits_and_variants": "Certaines procédures ne produisent pas de liste nominative.",
        "dependencies": ["explain.bicameralisme.navette"],
    },
    {
        "stable_id": "explain.bicameralisme.navette",
        "institution": ExplainEntry.Institution.BICAMERAL,
        "role_or_procedure": "navette",
        "title": "Bicamérisme et navette parlementaire",
        "short_text": (
            "Un texte circule entre Assemblée nationale et Sénat (navette) jusqu'à "
            "accord ou procédure de terminaison prévue par la Constitution."
        ),
        "detailed_text": (
            "Voter « pour » un texte en première lecture n'est pas équivalent à "
            "soutenir la loi finale promulguée : le texte peut être modifié en "
            "commission, en séance, à l'autre chambre, ou en commission mixte "
            "paritaire."
        ),
        "official_sources": [
            {
                "label": "Constitution — art. 45",
                "url": "https://www.conseil-constitutionnel.fr/le-bloc-de-constitutionnalite/texte-integral-de-la-constitution-du-4-octobre-1958-en-vigueur",
            }
        ],
        "limits_and_variants": "Les délais et lectures varient selon la procédure engagée.",
        "dependencies": [],
    },
    {
        "stable_id": "explain.groupe.parlementaire",
        "institution": ExplainEntry.Institution.PROCEDURE,
        "role_or_procedure": "groupe_parlementaire",
        "title": "Groupe parlementaire",
        "short_text": (
            "Un groupe rassemble des parlementaires pour l'organisation des travaux. "
            "La position du groupe n'est pas automatiquement celle de chaque membre."
        ),
        "detailed_text": (
            "Une parole collective portée par un porte-parole doit rester distincte "
            "d'une promesse personnelle. La répartition des votes du groupe peut être "
            "présentée avec réserve méthodologique."
        ),
        "official_sources": [
            {
                "label": "Assemblée — groupes politiques",
                "url": "https://www.assemblee-nationale.fr/dyn/les-groupes-politiques",
            }
        ],
        "limits_and_variants": "Les seuils et dénominations diffèrent entre chambres.",
        "dependencies": ["explain.assemblee.role"],
    },
    {
        "stable_id": "explain.commission",
        "institution": ExplainEntry.Institution.PROCEDURE,
        "role_or_procedure": "commission",
        "title": "Commission parlementaire",
        "short_text": (
            "Les commissions examinent les textes avant la séance publique et peuvent "
            "adopter un texte de commission distinct du texte déposé."
        ),
        "detailed_text": (
            "Un amendement adopté en commission, un article voté en séance et le texte "
            "adopté en lecture ne portent pas la même portée documentaire."
        ),
        "official_sources": [
            {
                "label": "Assemblée — commissions",
                "url": "https://www.assemblee-nationale.fr/dyn/commissions",
            }
        ],
        "limits_and_variants": "Certaines commissions spéciales ou mixtes ont des règles propres.",
        "dependencies": [],
    },
    {
        "stable_id": "explain.gouvernement.ministre",
        "institution": ExplainEntry.Institution.GOUVERNEMENT,
        "role_or_procedure": "ministre",
        "title": "Gouvernement et ministre",
        "short_text": (
            "Un membre du Gouvernement n'est pas un parlementaire au titre de cette "
            "fonction. Ses déclarations et les actes du Gouvernement se distinguent "
            "des votes d'élus."
        ),
        "detailed_text": (
            "La compatibilité mandat parlementaire / fonction gouvernementale suit "
            "les règles constitutionnelles. Une citation gouvernementale n'établit "
            "pas un vote individuel."
        ),
        "official_sources": [
            {
                "label": "Gouvernement.fr",
                "url": "https://www.gouvernement.fr/",
            }
        ],
        "limits_and_variants": "Distinguer élu, membre du Gouvernement et institution.",
        "dependencies": [],
    },
    {
        "stable_id": "explain.presidence",
        "institution": ExplainEntry.Institution.PRESIDENCE,
        "role_or_procedure": "president_republique",
        "title": "Président de la République",
        "short_text": (
            "Le Président de la République est une institution distincte du Parlement "
            "et du Gouvernement. Ses prises de parole publiques ne sont pas des votes."
        ),
        "detailed_text": (
            "Promulgation et publication relèvent du cycle juridique final, après "
            "l'adoption parlementaire. Elles ne remplacent pas l'analyse des scrutins."
        ),
        "official_sources": [
            {
                "label": "Élysée",
                "url": "https://www.elysee.fr/",
            }
        ],
        "limits_and_variants": "Ne pas confondre discours présidentiel et position parlementaire.",
        "dependencies": ["explain.procedure.promulgation"],
    },
    {
        "stable_id": "explain.procedure.amendement",
        "institution": ExplainEntry.Institution.PROCEDURE,
        "role_or_procedure": "amendement",
        "title": "Amendement",
        "short_text": (
            "Un amendement propose de modifier un texte. Son sort (adopté, rejeté, "
            "tombé) est distinct du vote sur l'ensemble."
        ),
        "detailed_text": (
            "« Pour un amendement », « pour un article », « pour un texte en première "
            "lecture » et « pour la loi finale » ne sont pas équivalents : chaque étape "
            "porte un objet procédural différent."
        ),
        "official_sources": [
            {
                "label": "Assemblée — amendements",
                "url": "https://www.assemblee-nationale.fr/dyn/15/amendements",
            }
        ],
        "limits_and_variants": "Les régimes d'irrecevabilité et d'examen varient.",
        "dependencies": ["explain.bicameralisme.navette"],
    },
    {
        "stable_id": "explain.procedure.scrutin",
        "institution": ExplainEntry.Institution.PROCEDURE,
        "role_or_procedure": "scrutin",
        "title": "Scrutin et vote nominatif",
        "short_text": (
            "Un scrutin peut être public nominatif ou non. Sans détail nominatif "
            "exploitable, la position individuelle reste inconnue."
        ),
        "detailed_text": (
            "Pourquoi un élu peut ne pas avoir de vote individuel exploitable : "
            "scrutin non nominatif, absence, non-participation, mise au point "
            "ultérieure, ou source officielle incomplète. Ces cas restent "
            "non concluants ; aucun bulletin n'est inventé."
        ),
        "official_sources": [
            {
                "label": "Assemblée — scrutins",
                "url": "https://www.assemblee-nationale.fr/dyn/scrutins",
            },
            {
                "label": "Sénat — scrutins publics",
                "url": "https://www.senat.fr/scrutin-public/",
            },
        ],
        "limits_and_variants": "Les mises au point ne réécrivent pas silencieusement l'observation initiale.",
        "dependencies": [],
    },
    {
        "stable_id": "explain.procedure.promulgation",
        "institution": ExplainEntry.Institution.PROCEDURE,
        "role_or_procedure": "promulgation",
        "title": "Texte adopté, promulgation et publication",
        "short_text": (
            "Après adoption parlementaire, la promulgation et la publication au JORF "
            "fixent l'état juridique ; l'entrée en vigueur peut être distincte."
        ),
        "detailed_text": (
            "Relier le cycle parlementaire à Légifrance/JORF permet de situer la "
            "version finale, sans confondre chaque lecture intermédiaire avec la loi "
            "promulguée."
        ),
        "official_sources": [
            {
                "label": "Légifrance",
                "url": "https://www.legifrance.gouv.fr/",
            }
        ],
        "limits_and_variants": "L'API PISTE Légifrance exige des identifiants ; open-data alternatif si admissible.",
        "dependencies": ["explain.bicameralisme.navette"],
    },
)


@transaction.atomic
def seed_explain_entries(*, verified_on: date | None = None) -> dict:
    verified_on = verified_on or date(2026, 9, 16)
    created = 0
    updated = 0
    for item in SEED:
        latest = (
            ExplainEntry.objects.filter(stable_id=item["stable_id"]).order_by("-version").first()
        )
        version = 1 if latest is None else latest.version
        preserve_inactive = latest is not None and not latest.active
        payload = {
            "institution": item["institution"],
            "role_or_procedure": item["role_or_procedure"],
            "title": item["title"],
            "short_text": item["short_text"],
            "detailed_text": item["detailed_text"],
            "official_sources": item["official_sources"],
            "verified_on": verified_on,
            "limits_and_variants": item["limits_and_variants"],
            "dependencies": item["dependencies"],
            "active": not preserve_inactive,
        }
        if latest is None:
            ExplainEntry.objects.create(stable_id=item["stable_id"], version=1, **payload)
            created += 1
        elif any(getattr(latest, key) != item[key] for key in (
            "institution",
            "role_or_procedure",
            "title",
            "short_text",
            "detailed_text",
            "official_sources",
            "limits_and_variants",
            "dependencies",
        )):
            ExplainEntry.objects.create(
                stable_id=item["stable_id"],
                version=latest.version + 1,
                **payload,
            )
            created += 1
        elif latest.active:
            for key, value in payload.items():
                if key == "active":
                    continue
                setattr(latest, key, value)
            latest.save()
            updated += 1
            version = latest.version
        else:
            # Inactif : seed idempotent sans réactivation silencieuse.
            version = latest.version
        _ = version
    return {"created": created, "updated": updated, "total_specs": len(SEED)}


def latest_entries(*, institution: str | None = None, include_inactive: bool = False) -> list[ExplainEntry]:
    """Révision courante par stable_id : version max d'abord, avant filtres UI.

    - include_inactive=False (export authorized) : si la révision courante est
      inactive, aucune ancienne version active n'est exportée à sa place.
    - include_inactive=True (export withdrawn) : la révision courante, même
      inactive, est exportable pour transmettre un retrait.
    """
    qs = ExplainEntry.objects.order_by("stable_id", "-version")
    current: dict[str, ExplainEntry] = {}
    for entry in qs:
        if entry.stable_id in current:
            continue
        current[entry.stable_id] = entry
    selected: list[ExplainEntry] = []
    for entry in current.values():
        if institution and entry.institution != institution:
            continue
        if not include_inactive and not entry.active:
            continue
        selected.append(entry)
    selected.sort(key=lambda e: (e.institution, e.stable_id))
    return selected


def entry_payload(entry: ExplainEntry) -> dict:
    return {
        "stable_id": entry.stable_id,
        "version": entry.version,
        "institution": entry.institution,
        "role_or_procedure": entry.role_or_procedure,
        "title": entry.title,
        "short_text": entry.short_text,
        "detailed_text": entry.detailed_text,
        "official_sources": entry.official_sources,
        "verified_on": entry.verified_on.isoformat() if entry.verified_on else None,
        "limits_and_variants": entry.limits_and_variants,
        "dependencies": entry.dependencies,
    }


def public_projection(*, authorization_state: str = "authorized") -> dict:
    """Projection publique minimale pour le Site (G08). Pas d'API privée exposée au navigateur.

    - authorized : révision courante active uniquement (pas de repli sur une ancienne).
    - withdrawn : révision courante, y compris inactive, marquée withdrawn
      (même numéro de version ; pas d'incrément artificiel à l'export).
    - draft : enveloppe sans entrées publiables.
    """
    if authorization_state not in {"authorized", "draft", "withdrawn"}:
        raise ValueError("explain_authorization_state_invalid")
    entries = []
    if authorization_state == "authorized":
        for entry in latest_entries(include_inactive=False):
            payload = entry_payload(entry)
            payload["authorization_state"] = "authorized"
            entries.append(payload)
    elif authorization_state == "withdrawn":
        for entry in latest_entries(include_inactive=True):
            if entry.active:
                continue
            payload = entry_payload(entry)
            payload["authorization_state"] = "withdrawn"
            entries.append(payload)
    return {
        "format": "dv.explain-public/0.1.0",
        "authorization_state": "authorized" if authorization_state == "withdrawn" else authorization_state,
        "partisan_commentary": False,
        "generated_at": timezone.now().isoformat(),
        "entries": entries,
    }


@transaction.atomic
def reauthorize_explain_entry(stable_id: str) -> ExplainEntry:
    """Réactivation explicite : nouvelle révision active, sans réécrire le seed silencieusement."""
    latest = (
        ExplainEntry.objects.filter(stable_id=stable_id).order_by("-version").first()
    )
    if latest is None:
        raise ValueError("explain_entry_missing")
    if latest.active:
        return latest
    payload = {
        "institution": latest.institution,
        "role_or_procedure": latest.role_or_procedure,
        "title": latest.title,
        "short_text": latest.short_text,
        "detailed_text": latest.detailed_text,
        "official_sources": latest.official_sources,
        "verified_on": latest.verified_on,
        "limits_and_variants": latest.limits_and_variants,
        "dependencies": latest.dependencies,
        "active": True,
    }
    return ExplainEntry.objects.create(
        stable_id=stable_id,
        version=latest.version + 1,
        **payload,
    )


def registry_extension_point() -> dict:
    """Extension future (ex. Parlement européen) via registry, pas texte codé en dur unique."""
    return {
        "allowed_institutions": [c.value for c in ExplainEntry.Institution],
        "extension": "other",
        "note": "Ajouter une institution via ExplainEntry.Institution.OTHER + stable_id dédié",
    }
