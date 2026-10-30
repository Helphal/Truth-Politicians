# Contrat candidat de diffusion minimale B006

La révision privée Atelier porte `dv.content/0.5.0-diffusion-draft.1`. Sa `diffusion_selection` versionnée `dv.diffusion-selection/1` contient des pointeurs `evidence_index`, des citations littérales et leurs crédits, puis une note publique. Les sources, versions, URL et locators viennent exclusivement du passage privé référencé. Le validateur refuse les clés inconnues, citations inventées, index invalides et une déclaration qui ne figure pas dans un extrait de sa source déclarée. Les limites de 1 200 caractères par extrait / 6 000 au total sont techniques et ne qualifient aucun droit.

L’archive de travail et `qualification_material` restent dans l’Atelier. La revue détachée privée emploie `dv.full-review/0.2.0-diffusion-draft.1` et couvre toujours tout le contenu privé, y compris la sélection. L’export ancien `authorize_and_queue` refuse ce profil pour empêcher une copie implicite de l’archive dans un ancien contrat.

L’unique transport de ce profil est `dv.verification-site/3`, avec `diffusion_snapshot` au format `dv.diffusion-snapshot/1`. `private_fingerprint` atteste l’entrée privée du vérificateur ; `diffusion_sha256` lie le contenu réellement transmis. Ces empreintes ne sont pas équivalentes. La revue Site porte uniquement sur cette projection et ne prétend pas avoir lu l’archive absente.

Le Site reçoit citations minimales, crédits, sources versionnées et les tableaux publics de contexte nécessaires. Une forme imbriquée fermée, dérivée des clés du contrat producteur, refuse les champs privés ajoutés ; elle n’ajoute aucun moteur de jugement éditorial. Toute source d’extrait doit être liée exactement aux références du snapshot. Le champ historique de modèle `documentary_snapshot` stocke la projection reçue sans migration.

Les protocoles v1/v2 restent lisibles historiquement. Un upsert v1/v2 après un upsert v3 est refusé ; le journal de retrait est monotone. L’ancien récepteur B005 refuse v3 comme inconnu. Les modifications de citation, crédit, note ou source changent l’entrée examinée, imposent une nouvelle révision et rendent les accords antérieurs inutilisables.

Aucun avis humain réel, droit global ou publication n’est créé par la sélection. Les exemples et tests sont SYNTH. Les six propositions réelles sont destinées aux aperçus privés, avec leurs réserves explicites.
