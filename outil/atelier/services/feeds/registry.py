"""Catalogue d'alimentation permanente BUILD-002 (FEEDS-01)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FeedSpec:
    feed_id: str
    authority: str
    family: str
    cadence: str  # fast | hourly | daily | nightly | manual
    mechanism: str
    discovery_url: str
    document_url: str = ""
    terms_url: str = ""
    auth_required: bool = False
    default_active: bool = False
    rights_status: str = "open"
    coverage_note: str = ""
    block_reason: str = ""
    historical_scope: str = ""
    adapter: str = "http_snapshot"
    extra: dict[str, Any] = field(default_factory=dict)


# Cadences par défaut (secondes) — configurables via settings.
DEFAULT_CADENCE_SECONDS = {
    "fast": 20 * 60,
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "nightly": 24 * 60 * 60,
    "manual": 0,
}


ASSEMBLEE_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec(
        feed_id="AN_AMO10_XVII",
        authority="Assemblée nationale",
        family="acteurs_mandats_organes",
        cadence="daily",
        mechanism="opendata_zip_json",
        discovery_url="https://data.assemblee-nationale.fr/acteurs/deputes-en-exercice",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/17/amo/deputes_actifs_mandats_actifs_organes/AMO10_deputes_actifs_mandats_actifs_organes.json.zip",
        terms_url="https://data.assemblee-nationale.fr/licence-ouverte-open-licence",
        default_active=True,
        coverage_note="Députés actifs, mandats, groupes, organes XVIIe",
        historical_scope="XVII",
        adapter="an_amo",
    ),
    FeedSpec(
        feed_id="AN_VOTES_XVII",
        authority="Assemblée nationale",
        family="scrutins",
        cadence="daily",
        mechanism="opendata_zip_json",
        discovery_url="https://data.assemblee-nationale.fr/travaux-parlementaires/votes",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/17/loi/scrutins/Scrutins.json.zip",
        terms_url="https://data.assemblee-nationale.fr/licence",
        default_active=True,
        coverage_note="Scrutins + positions nominatives législature courante",
        historical_scope="XVII",
        adapter="an_votes",
    ),
    FeedSpec(
        feed_id="AN_VOTES_XVI",
        authority="Assemblée nationale",
        family="scrutins_archive",
        cadence="daily",
        mechanism="opendata_zip_json",
        discovery_url="https://data.assemblee-nationale.fr/archives-16e/votes",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/16/loi/scrutins/Scrutins.json.zip",
        terms_url="https://data.assemblee-nationale.fr/licence",
        default_active=True,
        coverage_note="Archive XVIe ; backfill maîtrisé",
        historical_scope="XVI",
        adapter="an_votes",
    ),
    FeedSpec(
        feed_id="AN_VOTES_XV",
        authority="Assemblée nationale",
        family="scrutins_archive",
        cadence="manual",
        mechanism="opendata_zip_json",
        discovery_url="https://data.assemblee-nationale.fr/",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/15/loi/scrutins/Scrutins.json.zip",
        terms_url="https://data.assemblee-nationale.fr/licence",
        default_active=False,
        coverage_note="XVe backfill optionnel ; non activé par défaut (stockage)",
        historical_scope="XV",
        adapter="an_votes",
    ),
    FeedSpec(
        feed_id="AN_AMENDEMENTS_XVII",
        authority="Assemblée nationale",
        family="amendements",
        cadence="hourly",
        mechanism="opendata_zip_json",
        discovery_url="https://data.assemblee-nationale.fr/travaux-parlementaires/amendements/tous-les-amendements",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/17/loi/amendements_div_legis/Amendements.json.zip",
        terms_url="https://data.assemblee-nationale.fr/licence",
        default_active=True,
        coverage_note="Amendements dossier/auteurs/objet/sort (export amendements_div_legis)",
        adapter="an_amendements",
    ),
    FeedSpec(
        feed_id="AN_DOSSIERS_XVII",
        authority="Assemblée nationale",
        family="dossiers_legislatifs",
        cadence="hourly",
        mechanism="opendata_zip_json",
        discovery_url="https://data.assemblee-nationale.fr/travaux-parlementaires/dossiers-legislatifs",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/17/loi/dossiers_legislatifs/Dossiers_Legislatifs.json.zip",
        terms_url="https://data.assemblee-nationale.fr/licence",
        default_active=True,
        coverage_note="Dossiers, textes, étapes",
        adapter="an_dossiers",
    ),
    FeedSpec(
        feed_id="AN_COMPTES_RENDUS_XVII",
        authority="Assemblée nationale",
        family="comptes_rendus",
        cadence="hourly",
        mechanism="opendata_html_index",
        discovery_url="https://www.assemblee-nationale.fr/dyn/17/comptes-rendus/seance",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/17/vp/syceronbrut/syseron.xml.zip",
        terms_url="https://data.assemblee-nationale.fr/licence",
        default_active=True,
        coverage_note="Débats de séance (Syceronbrut XML zip)",
        adapter="an_cr",
    ),
    FeedSpec(
        feed_id="AN_QUESTIONS_XVII",
        authority="Assemblée nationale",
        family="questions",
        cadence="hourly",
        mechanism="opendata_zip_json",
        discovery_url="https://data.assemblee-nationale.fr/travaux-parlementaires/questions",
        document_url="https://data.assemblee-nationale.fr/static/openData/repository/17/questions/questions_ecrites/Questions_ecrites.json.zip",
        terms_url="https://data.assemblee-nationale.fr/licence",
        default_active=True,
        coverage_note="Questions écrites / orales / QAG lorsque présentes",
        adapter="an_questions",
    ),
)

SENAT_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec(
        feed_id="SENAT_SENATEURS",
        authority="Sénat",
        family="acteurs_mandats_organes",
        cadence="daily",
        mechanism="opendata_json",
        discovery_url="https://data.senat.fr/les-senateurs/",
        document_url="https://data.senat.fr/data/senateurs/ODSEN_GENERAL.json",
        terms_url="https://data.senat.fr/licence/",
        default_active=True,
        coverage_note="Sénateurs (ODSEN_GENERAL) + API senateurs.json disponible",
        adapter="senat_acteurs",
        rights_status="excerpt_allowed",
    ),
    FeedSpec(
        feed_id="SENAT_DOSLEG",
        authority="Sénat",
        family="dossiers_legislatifs",
        cadence="daily",
        mechanism="opendata_dosleg",
        discovery_url="https://data.senat.fr/aide/travaux-legislatifs-base-dosleg/",
        document_url="https://www.senat.fr/dossiers-legislatifs/textes-recents.html",
        terms_url="https://data.senat.fr/licence/",
        default_active=True,
        coverage_note="DOSLEG : dossiers/textes via pages Sénat publiques (export bulk à qualifier)",
        adapter="senat_dosleg",
        rights_status="excerpt_allowed",
    ),
    FeedSpec(
        feed_id="SENAT_AMELI",
        authority="Sénat",
        family="amendements",
        cadence="hourly",
        mechanism="opendata_ameli",
        discovery_url="https://data.senat.fr/category/ameli/",
        document_url="https://data.senat.fr/category/ameli/",
        terms_url="https://data.senat.fr/licence/",
        default_active=True,
        coverage_note="AMELI : portail open-data catégorie amendements",
        adapter="senat_ameli",
        rights_status="excerpt_allowed",
    ),
    FeedSpec(
        feed_id="SENAT_AKN_DEPOTS",
        authority="Sénat",
        family="index_textes_akoma_ntoso",
        cadence="daily",
        mechanism="akoma_ntoso_xml",
        discovery_url="https://data.senat.fr/les-donnees-dispositifs-des-textes-monalisa/",
        document_url="https://www.senat.fr/akomantoso/depots.xml",
        terms_url="https://data.senat.fr/licence/",
        default_active=True,
        coverage_note="Monalisa / Akoma Ntoso index dépôts",
        adapter="senat_akn",
        rights_status="excerpt_allowed",
    ),
    FeedSpec(
        feed_id="SENAT_COMPTES_RENDUS",
        authority="Sénat",
        family="comptes_rendus",
        cadence="hourly",
        mechanism="html_seances",
        discovery_url="https://www.senat.fr/seances/seances.html",
        document_url="https://www.senat.fr/seances/seances.html",
        terms_url="https://data.senat.fr/licence/",
        default_active=True,
        coverage_note="Séances / interventions / orateurs",
        adapter="senat_cr",
        rights_status="excerpt_allowed",
    ),
    FeedSpec(
        feed_id="SENAT_QUESTIONS_BASILE",
        authority="Sénat",
        family="questions",
        cadence="hourly",
        mechanism="opendata_basile",
        discovery_url="https://data.senat.fr/category/questions/",
        document_url="https://www.senat.fr/basile/baseRequete.do",
        terms_url="https://data.senat.fr/licence/",
        default_active=True,
        coverage_note="Basile / questions : formulaire et catégorie open-data",
        adapter="senat_questions",
        rights_status="excerpt_allowed",
    ),
    FeedSpec(
        feed_id="SENAT_SCRUTINS_PUBLICS",
        authority="Sénat",
        family="scrutins",
        cadence="daily",
        mechanism="html_scrutin_public",
        discovery_url="https://www.senat.fr/scrutin-public/scr2025.html",
        document_url="https://www.senat.fr/scrutin-public/scr2025.html",
        terms_url="https://data.senat.fr/licence/",
        default_active=True,
        coverage_note="Index scrutins publics 2025 ; nominatif selon pages individuelles",
        adapter="senat_scrutins",
        rights_status="excerpt_allowed",
    ),
)

LEGIFRANCE_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec(
        feed_id="LEGIFRANCE_JORF_PISTE",
        authority="Légifrance / JORF",
        family="texte_promulgue",
        cadence="daily",
        mechanism="piste_api",
        discovery_url="https://www.legifrance.gouv.fr/",
        document_url="https://api.piste.gouv.fr/dila/legifrance/lf-engine-app/",
        terms_url="https://piste.gouv.fr/",
        auth_required=True,
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="API stable PISTE ; credentials absents",
        block_reason="blocked_credentials_or_provider_terms",
        adapter="legifrance_piste",
        extra={"requires_env": ["DV_LEGIFRANCE_CLIENT_ID", "DV_LEGIFRANCE_CLIENT_SECRET"]},
    ),
    FeedSpec(
        feed_id="LEGIFRANCE_OPEN_DATA_ELI",
        authority="Légifrance / JORF",
        family="texte_promulgue_opendata",
        cadence="daily",
        mechanism="opendata_eli",
        discovery_url="https://www.data.gouv.fr/fr/datasets/legi-codes-lois-et-reglements-consolides/",
        document_url="",
        terms_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
        default_active=False,
        coverage_note="Open-data public non authentifié si contrat officiel le permet ; sinon resté inactif",
        adapter="legifrance_opendata",
        rights_status="open",
    ),
)

SPEECH_INSTITUTIONAL_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec(
        feed_id="ELYSEE_DISCOURS",
        authority="Élysée",
        family="discours_institutionnels",
        cadence="hourly",
        mechanism="html_index",
        discovery_url="https://www.elysee.fr/recherche?q=&tab=discours",
        document_url="https://www.elysee.fr/",
        terms_url="https://www.elysee.fr/",
        default_active=True,
        rights_status="excerpt_allowed",
        coverage_note="Discours, conférences, communiqués publics",
        adapter="elysee_speech",
    ),
    FeedSpec(
        feed_id="GOUV_DISCOURS",
        authority="Gouvernement",
        family="discours_institutionnels",
        cadence="hourly",
        mechanism="html_index",
        discovery_url="https://www.gouvernement.fr/",
        document_url="https://www.gouvernement.fr/",
        terms_url="https://www.gouvernement.fr/",
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="Home/sitemap/RSS info.gouv.fr en 403 WAF ; aucune voie publique exploitable sans contournement",
        block_reason="provider_unavailable",
        adapter="gouv_speech",
    ),
)

MEDIA_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec(
        feed_id="PRESS_RSS_GENERIC",
        authority="Presse / web",
        family="rss_atom",
        cadence="fast",
        mechanism="rss_atom",
        discovery_url="https://www.example.invalid/rss",
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="Registry RSS/Atom configurable ; pas de paywall bypass",
        adapter="press_rss",
        block_reason="configuration_incomplete",
    ),
    FeedSpec(
        feed_id="YOUTUBE_OFFICIAL_CHANNELS",
        authority="YouTube / vidéo",
        family="video_metadata",
        cadence="hourly",
        mechanism="public_feed_metadata",
        discovery_url="https://www.youtube.com/",
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="Métadonnées + sous-titres publics ; pas de vidéo complète par défaut",
        adapter="youtube_meta",
        auth_required=False,
        block_reason="configuration_incomplete",
    ),
    FeedSpec(
        feed_id="PODCAST_RSS",
        authority="TV / radio / podcasts",
        family="podcast_rss",
        cadence="hourly",
        mechanism="rss_podcast",
        discovery_url="https://www.example.invalid/podcast.xml",
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="RSS podcast + transcriptions publiques",
        adapter="podcast_rss",
        block_reason="configuration_incomplete",
    ),
)

SOCIAL_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec(
        feed_id="BLUESKY_PUBLIC",
        authority="Bluesky",
        family="social",
        cadence="fast",
        mechanism="atproto_public",
        discovery_url="https://public.api.bsky.app/",
        document_url="https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="API publique AT Proto ; comptes officiels configurables",
        adapter="bluesky_public",
        block_reason="configuration_incomplete",
    ),
    FeedSpec(
        feed_id="X_TWITTER",
        authority="X / Twitter",
        family="social",
        cadence="fast",
        mechanism="official_api",
        discovery_url="https://developer.x.com/",
        auth_required=True,
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="Contrat + mocks ; aucun compte/token créé",
        adapter="social_blocked",
        block_reason="blocked_credentials_or_provider_terms",
        extra={"provider": "x"},
    ),
    FeedSpec(
        feed_id="META_FACEBOOK",
        authority="Facebook / Meta",
        family="social",
        cadence="fast",
        mechanism="official_api",
        discovery_url="https://developers.facebook.com/",
        auth_required=True,
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="Contrat + mocks ; aucun compte/token créé",
        adapter="social_blocked",
        block_reason="blocked_credentials_or_provider_terms",
        extra={"provider": "facebook"},
    ),
    FeedSpec(
        feed_id="META_INSTAGRAM",
        authority="Instagram / Meta",
        family="social",
        cadence="fast",
        mechanism="official_api",
        discovery_url="https://developers.facebook.com/docs/instagram-api/",
        auth_required=True,
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="Contrat + mocks ; aucun compte/token créé",
        adapter="social_blocked",
        block_reason="blocked_credentials_or_provider_terms",
        extra={"provider": "instagram"},
    ),
    FeedSpec(
        feed_id="TIKTOK",
        authority="TikTok",
        family="social",
        cadence="fast",
        mechanism="official_api",
        discovery_url="https://developers.tiktok.com/",
        auth_required=True,
        default_active=False,
        rights_status="excerpt_allowed",
        coverage_note="Contrat + mocks ; aucun compte/token créé",
        adapter="social_blocked",
        block_reason="blocked_credentials_or_provider_terms",
        extra={"provider": "tiktok"},
    ),
)


def all_feed_specs() -> tuple[FeedSpec, ...]:
    return (
        ASSEMBLEE_FEEDS
        + SENAT_FEEDS
        + LEGIFRANCE_FEEDS
        + SPEECH_INSTITUTIONAL_FEEDS
        + MEDIA_FEEDS
        + SOCIAL_FEEDS
    )


def feed_by_id(feed_id: str) -> FeedSpec:
    for spec in all_feed_specs():
        if spec.feed_id == feed_id:
            return spec
    raise KeyError(feed_id)


def specs_for_cadence(cadence: str) -> tuple[FeedSpec, ...]:
    return tuple(spec for spec in all_feed_specs() if spec.cadence == cadence)
