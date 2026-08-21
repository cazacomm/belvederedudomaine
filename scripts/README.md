# Blog automatique — Le Belvédère du Domaine

Un article est généré et publié **chaque lundi à 09:00 UTC** (11h en heure d'été
française, 10h en hiver) par `.github/workflows/blog-auto.yml`, sans intervention.

## 1. Ajouter la clé API (une seule fois)

1. Créer une clé sur <https://platform.openai.com/api-keys>
2. GitHub → **Settings → Secrets and variables → Actions → New repository secret**
3. Nom : `OPENAI_API_KEY` — Valeur : la clé (`sk-…`)

Vérifier aussi **Settings → Actions → General → Workflow permissions** :
**Read and write permissions** doit être actif, sinon le workflow ne peut pas pousser.

## 2. Lancer manuellement

**Depuis GitHub** — onglet *Actions* → *Blog auto — Le Belvédère du Domaine* →
*Run workflow*. Deux champs : `dry_run` (simuler sans publier) et `rewrite`
(slug d'un article existant à régénérer).

**En local :**

```bash
export OPENAI_API_KEY="sk-…"
pip install openai

python3 scripts/generate-article.py --dry-run --mock   # assemblage seul, sans API
python3 scripts/generate-article.py --dry-run          # génère et affiche, n'écrit rien
python3 scripts/generate-article.py                    # génère et publie
python3 scripts/generate-article.py --rewrite <slug>   # régénère un article existant
```

Codes de sortie : `0` succès · `78` aucun nouveau sujet (arrêt propre) · `1` erreur.

## 3. Le principe : le modèle n'écrit plus de HTML

C'est le cœur du fonctionnement, et ce qui change tout par rapport à la version
précédente.

Le modèle ne reçoit plus de gabarit à recopier. Il rend un **JSON éditorial**
(titre, chapô, sections `h2`/`h3`, paragraphes, listes, FAQ) et **le script
assemble la page** : `<head>`, meta, canonical, Open Graph, Twitter Card, les
trois blocs JSON-LD, le fil d'Ariane, le marqueur d'idempotence, le header, le
footer et le bloc CTA viennent du gabarit relu et du script.

Conséquence directe sur le volume : auparavant les deux tiers des tokens de sortie
partaient en balisage, ce qui plafonnait le corps rédigé autour de **400 mots**
malgré une consigne de 1300. Les tokens vont désormais entièrement au texte.

Conséquence sur la sécurité : le modèle ne fournit que du texte brut, échappé à
l'assemblage. Seuls `**gras**` et `[libellé](/chemin)` sont interprétés, et les
liens sont restreints aux chemins commençant par `/` — un lien externe est
structurellement impossible.

### Le gabarit reste relu, jamais dupliqué

`split_template()` découpe l'article de référence
(`reference_article_slug` dans `blog-config.json`) selon les conventions HTML de
**ce site**, qui diffèrent de celles d'autres sites du même pipeline :

| Élément | Convention de ce site |
|---|---|
| Début des JSON-LD | première balise `<script type="application/ld+json">` (pas de commentaire `<!-- Article -->`) |
| Balise principale | `<main id="contenu">` (et non `<main>`) |
| Fil d'Ariane | `<nav class="breadcrumb">` situé **avant** le `<main>`, donc réécrit dans le morceau « header » |
| Bloc CTA | `<div class="cta-box">` contenant un `<div class="cta-actions">` imbriqué — extrait en comptant les balises, pas par regex |
| FAQ | `<details>` / `<summary>` (et non des `<p class="faq-q">`) |
| Liste d'articles | `<div class="post-list">` contenant des `<a class="post-card">` |

Si le gabarit évolue (nouveau footer, nouvelle police), les articles suivants
suivent automatiquement.

## 4. Rattrapage et budget d'appels

Le script valide le contenu reçu : volume, nombre de questions, longueur du
`title` et de la description, maillage interne. **Toute erreur corrigeable par le
modèle** déclenche une reprise — pas seulement un volume insuffisant.

- Plafond : **3 appels** par article (`MAX_CALLS`).
- Chaque reprise repart de la **meilleure copie obtenue**, pas de la dernière :
  le modèle développe un texte déjà long au lieu de repartir d'un plus court.
- Si la nouvelle copie n'est pas meilleure, l'ancienne est conservée.
- Si des erreurs subsistent au bout des 3 appels : **aucun fichier n'est écrit**,
  code 1, le workflow ne committe rien.

Cible : **1200–1500 mots** pour le corps hors FAQ (`PROMPT_MIN/MAX_WORDS`),
avec des bornes de validation plus larges (900–1900) pour ne pas rejeter une
bonne copie à 20 mots près.

## 5. Idempotence et garde-fous

- Marqueur `<!-- belvedere65-topic: N -->` dans chaque article généré.
- Slug **déterministe** : le même titre produit toujours le même slug.
- Si le dossier du slug existe déjà → code 78, rien n'est écrasé
  (`--rewrite` pour forcer).
- `blog/index.html`, `sitemap.xml`, `rss.xml` et `llms.txt` sont mis à jour de
  façon idempotente : une URL déjà présente n'est jamais dupliquée.
- Un sujet marqué « publié » dans le tableau de `BLOG_WORKFLOW.md` est ignoré —
  c'est ainsi que l'article écrit à la main, antérieur au marqueur, reste exclu.
- Garde-fous éditoriaux injectés dans le prompt : interdiction d'inventer prix,
  chiffres, noms de clients, dates de fondation, réglementations et adresses.
  Les seuls faits chiffrés autorisés sont ceux listés dans `facts`
  (`blog-config.json`).
- Contrôles d'assemblage avant écriture : DOCTYPE, `</html>`, un seul `<h1>`,
  canonical exact, `<main>` équilibré, 3 blocs JSON-LD valides, 5 questions de
  FAQ, et **parité stricte entre la FAQ affichée et le `FAQPage`**.

## 6. Configuration

`blog-config.json` porte 19 clés obligatoires, vérifiées au démarrage
(`REQUIRED_KEYS`) — le script s'arrête avec un message clair si l'une manque :

`site_name`, `site_url`, `sector`, `location`, `geo_keywords`, `tone`, `author`,
`target_word_count`, `faq_questions_count`, `language`, `model`, `temperature`,
`topic_marker_prefix`, `og_image`, `logo_path`, `default_article_section`,
`internal_link_targets`, `reference_article_slug`, `facts`.

Le bloc `nap` (facultatif) alimente le `publisher` des JSON-LD.

## 7. Coût

Avec `gpt-4o` : environ 2 500 jetons d'entrée et 4 000 de sortie par appel.

| | Par article | Par an (52 lundis) |
|---|---|---|
| 1 appel (cas courant) | ≈ 0,046 $ | ≈ 2,40 $ |
| 3 appels (plafond) | ≈ 0,14 $ | ≈ 7,30 $ |

Soit quelques euros par an dans le pire cas. Ordre de grandeur indicatif :
tarifs à jour sur <https://openai.com/api/pricing/>.

À noter : la réserve de sujets couvre les lundis restants du tableau §7 de
`BLOG_WORKFLOW.md`. Une fois épuisée, le workflow sort en code 78 chaque lundi
sans rien publier — il suffit d'ajouter des lignes au tableau.

## 8. Relecture

Le workflow publie sans validation humaine. Deux réflexes :

- s'abonner aux notifications d'échec (Actions → *Watch*) ;
- relire l'article du lundi dans la journée. En cas de problème,
  `git revert` du commit `chore(blog): article auto du …` retire l'article **et**
  les mises à jour de sitemap, RSS, liste et `llms.txt` en une commande ;
  `--rewrite <slug>` permet de le régénérer sans changer d'URL.
