# Pipeline éditorial V2

Le profil `personal` active la V2 avec `editorial.v2.enabled: true`. Les autres profils
et les tests historiques conservent la chaîne précédente.

1. Les flux RSS fournissent des candidats pour les rubriques d'actualité. Histoire et
   Mythologies & Religions tirent leur sujet de `topics.v2.yaml`. Une entrée de cette
   banque sert de requête, jamais de preuve envoyée au rédacteur.
2. Si le dossier initial est pauvre, la recherche Tavily explore des angles distincts.
   Les résultats traversent le filtre documentaire commun et gardent URL, organisme,
   titre, date et contenu. Le dossier doit toujours atteindre 900 caractères avant
   le premier appel LLM.
3. GPT-4.1 mini produit un draft, puis une critique JSON séparée. La critique ne
   rédige pas l'article. Ses requêtes éventuelles déclenchent au plus cinq recherches
   complémentaires, sans répéter une requête de la première phase.
4. GPT-4.1 mini réécrit enfin le texte avec le draft, la critique et le dossier mis à
   jour. Les contrôles de citations, de longueur, de sections et de doublons restent
   bloquants. Seules les sources citées sont imprimées dans le PDF.

Les plafonds par article et par édition figurent dans `config.personal.example.yaml`.
`output/state/tavily-credits.json` suit les crédits **locaux connus** du mois Sydney ;
ce compteur n'est pas le solde du compte Tavily. Sur GitHub Actions, le cache du
workflow journal conserve ce fichier et `output/state/timeless-topics.json` entre
deux exécutions. Une absence ou une éviction du cache fait repartir le compteur
local à zéro ; le quota réel Tavily reste appliqué par le fournisseur.

Le coût API du PDF est théorique : tarifs configurables dans `api_cost`, tokens lus
dans `usage` des réponses Chat Completions et crédits Tavily connus ou une unité par
recherche `basic`. Les remises de cache, quotas gratuits et conditions du compte
ne sont pas déduits. Si une réponse ne fournit pas l'usage token, l'estimation
globale est indiquée comme indisponible.
