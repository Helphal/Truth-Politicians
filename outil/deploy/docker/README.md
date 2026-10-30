# Truth Politicians — exemple Docker local

Cet exemple installe l’outil et son interface de travail sur la machine locale. Il ne contient aucun site éditorial public. Docker est facultatif ; l’installation Python est décrite dans le [README de l’outil](../../README.md).

Le profil utilise SQLite, un volume privé pour les données et un volume distinct pour les fichiers statiques. Seul le proxy publie un port, lié à `127.0.0.1`. Le runtime n’est jamais monté dans le proxy : celui-ci ne voit que les statiques. Les collectes applicatives restent désactivées (`DV_NETWORK_MODE=disabled`) et aucun site de destination n’est configuré.

## Démarrage

Depuis le répertoire `outil/` :

```sh
docker compose -f deploy/docker/compose.yaml up --build -d
docker compose -f deploy/docker/compose.yaml exec atelier python manage.py bootstrap_initial_accounts
```

Ouvrir `http://127.0.0.1:8000`. La commande d’initialisation affiche une seule fois les accès temporaires ; elle refuse de recréer un administrateur existant. Conserver ces accès hors du dépôt. Aucun compte ni mot de passe personnel n’est fourni.

Le port peut être changé avec la variable `TP_LOCAL_PORT`. Les images et dépendances applicatives fournies ciblent ici Linux amd64 ; une autre architecture exige un fichier de dépendances et une image compatibles. Le scheduler est un profil optionnel :

```sh
docker compose -f deploy/docker/compose.yaml --profile scheduler up -d
```

## Arrêt et persistance

```sh
docker compose -f deploy/docker/compose.yaml down
```

Les volumes restent conservés. L’option `--volumes` supprimerait les données ; elle ne fait pas partie de la commande d’arrêt.

## Hébergement privé

Ce profil local ne doit pas être exposé sur Internet. Pour un service privé accessible à distance, configurer un proxy TLS, les hôtes autorisés, des secrets privés, les permissions, les sauvegardes et l’entrée d’administration de confiance selon [SECURITE.md](../../docs/SECURITE.md), puis vérifier le déploiement réel. Le fait de changer l’adresse d’écoute ne réalise pas cette configuration.

Le raccordement à un site est indépendant : il passe par les exports ou un récepteur compatible avec les contrats. L’outil reste utilisable sans site. Cet exemple n’établit aucune preuve de déploiement ni de qualification Docker sur la machine de l’utilisateur.
