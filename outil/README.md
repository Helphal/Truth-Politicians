# Truth Politicians — outil

Application Django de collecte, normalisation, recherche documentaire, préparation éditoriale et revue. Son interface web est l'espace de travail interne de l'outil. Un site de publication est facultatif et se raccorde séparément.

## Installation locale

Prérequis : Python 3.12. Utiliser une base neuve pour ce premier démarrage. Les commandes suivantes sont exécutées depuis le dossier `outil/`.

### Linux et macOS

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
mkdir -p runtime
python manage.py migrate
python manage.py seed_sources
python manage.py bootstrap_initial_accounts
python manage.py check
python manage.py runserver 127.0.0.1:8000 --insecure --noreload
```

### Windows — PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-qualification-windows-py312.lock
New-Item -ItemType Directory -Force runtime | Out-Null
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_sources
.\.venv\Scripts\python.exe manage.py bootstrap_initial_accounts
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --insecure --noreload
```

Ouvrir `http://127.0.0.1:8000/connexion/`. Le bootstrap crée un administrateur et un éditeur avec identifiants générés, affichés une seule fois dans le terminal. Les conserver dans un gestionnaire de mots de passe ; le premier accès impose leur changement. Relancer le bootstrap sur une installation déjà initialisée est refusé par défaut.

`seed_sources` enregistre les références des sources, sans lancer une collecte ni ajouter un corpus de travail. Le réseau et les envois restent désactivés. Le serveur de développement écoute seulement sur cette machine. L'option `--insecure` sert les fichiers statiques locaux avec `DEBUG` désactivé ; ce mode de développement ne doit pas être exposé à Internet.

Un [exemple Docker facultatif](deploy/docker/) est également fourni. Les verrous de qualification sont propres à leur plateforme ; leur présence ne constitue pas un test réalisé sur toutes ces plateformes.

## Configuration

Configurer les variables dans l'environnement du processus ou dans le gestionnaire de services. Le lancement Python ne charge pas automatiquement un fichier `.env`.

| Paramètre | Usage |
|---|---|
| `DV_ENV` | `local` par défaut ; `test`, `private` ou `production` selon l'installation. |
| `DV_RUNTIME_DIR` | Répertoire local de la base, des preuves et des fichiers de travail. |
| `DV_DATABASE_PATH` | Emplacement facultatif de la base SQLite. |
| `DV_SECRET_KEY` | Secret propre à l'installation, obligatoire hors local/test. |
| `DV_ALLOWED_HOSTS` | Hôtes de l'outil autorisés hors local/test. |
| `DV_REVIEW_ATTESTATION_KEY` | Clé privée de revue requise hors local/test. |
| `DV_FINAL_REVIEW_AUTHORITY_INTERNAL_UID` | Réviseur final autorisé ; vide signifie absence d'autorité de revue finale. |
| `DV_NETWORK_MODE` | `disabled` par défaut ; toute activation est un choix explicite de l'administrateur. |

Les installations privées ou de production nécessitent en plus une entrée privée, une configuration HTTPS/proxy cohérente et leurs propres comptes et clés. Voir [SECURITE.md](docs/SECURITE.md). Aucune adresse d'hébergement ou de publication n'est préconfigurée.

Les noms techniques `DV_*`, `dv.*` et `dv_project` restent stables pour préserver les contrats et le schéma des données. Les textes d'interface et la documentation utilisent le nom Truth Politicians.

## Dossiers, revue et export

Le [comparateur](docs/COMPARATEUR_PAROLE_VOTES.md) permet de partir d'une déclaration ou d'une mesure. Les rapprochements restent soumis à leurs preuves, aux versions des textes et aux contrôles éditoriaux. Les comparaisons interprétatives exigent une validation humaine sur leur version exacte.

Les exports sont des données destinées à un récepteur choisi par l'administrateur. La commande `python manage.py export_outbox --help` décrit l'export local de la boîte d'envoi ; elle n'autorise pas la publication. Les [contrats](docs/CONTRAT_SITE_CANDIDAT.md) décrivent la validation, les signatures, les reçus et les conditions de raccordement.

Le connecteur WordPress facultatif exige `DV_WORDPRESS_ENDPOINT`, `DV_WORDPRESS_USERNAME`, `DV_WORDPRESS_APP_PASSWORD` et `DV_WORDPRESS_ALLOWED_HOSTS`. La liste d'hôtes est vide par défaut. L'activation du réseau, l'autorisation d'envoi et la réconciliation du récepteur restent des contrôles distincts ; remplir ces quatre paramètres ne suffit pas à publier.

Un adaptateur d'analyse externe facultatif utilise `DV_ANALYSIS_PROVIDER_ENDPOINT` et `DV_ANALYSIS_PROVIDER_TOKEN`. Aucun fournisseur n'est inclus ou activé. Les propositions retournées restent non validées ; un modèle ne remplace pas les preuves ni la revue humaine.

## Vérification et limites

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
```

Le [bilan de validation de cette révision](../VALIDATION.md) donne le profil de tests exécuté. Les tests d'intégration externe et certaines recettes documentaires historiques exigent des pièces absentes de la distribution ; une suite complète n'est pas présentée comme autonome.

Les exemples synthétiques sont marqués comme tels. Aucun résultat politique réel, compte de production, fournisseur payant ou droit de publication n'est créé par l'installation.

Pour une base existante, préparer une sauvegarde et vérifier la compatibilité des migrations et des attestations avant changement de version. Les changements de noms de l'adaptateur d'analyse et des métadonnées de capture ne réattestent pas automatiquement d'anciennes propositions ou preuves.
