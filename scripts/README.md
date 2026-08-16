# Automatisation du blog

Un article est généré et publié **chaque lundi à 09:00 UTC** (11h l'été en France,
10h l'hiver) par le workflow `.github/workflows/blog-auto.yml`, sans intervention.

## 1. Ajouter la clé API (à faire une seule fois)

1. Créer une clé sur <https://platform.openai.com/api-keys>
2. Sur GitHub : **Settings → Secrets and variables → Actions → New repository secret**
3. Nom : `OPENAI_API_KEY` — Valeur : la clé (`sk-…`)

Vérifier aussi, dans **Settings → Actions → General → Workflow permissions**, que
l'option **Read and write permissions** est active : le workflow doit pouvoir pousser
son commit.

## 2. Lancer manuellement

**Depuis GitHub** — onglet *Actions* → *Blog — article automatique* → *Run workflow*.
Deux options : `topic` (forcer un numéro de sujet) et `dry_run` (générer sans publier).

**En local :**

```bash
export OPENAI_API_KEY="sk-…"
pip install openai

python3 scripts/generate-article.py --dry-run     # génère et affiche, n'écrit rien
python3 scripts/generate-article.py               # génère et écrit les fichiers
python3 scripts/generate-article.py --topic 3     # force le sujet n°3
```

Codes de sortie : `0` succès · `78` aucun nouveau sujet (arrêt propre) · `1` erreur.

## 3. Ce que fait le script

1. Lit `blog-config.json` (identité, NAP, faits autorisés, réglages du modèle).
2. Extrait les 12 sujets du tableau §7 de `BLOG_WORKFLOW.md`.
3. Scanne `blog/*/index.html`, relève les marqueurs `<!-- belvedere65-topic: N -->`
   et retient le **premier sujet non traité**, dans l'ordre.
4. **Relit le gabarit depuis l'article publié le plus ancien** — aucun template n'est
   dupliqué dans le script. En-tête, pied de page, CSS et structure restent identiques.
5. Appelle OpenAI (`gpt-4o-mini`, `temperature` 0.7, réponse JSON stricte).
6. Écrit `blog/<slug>/index.html`, ajoute la carte dans `blog/index.html`, met à jour
   `sitemap.xml` et `rss.xml`.

### Garde-fous

- **Idempotence** : marqueur de sujet dans chaque article ; si le dossier du slug
  existe déjà, arrêt en code 78 sans rien toucher ; les insertions dans
  `blog/index.html`, `sitemap.xml` et `rss.xml` sont ignorées si l'URL y figure déjà.
- **Contrôles avant écriture** : un seul `<h1>`, JSON-LD valide, canonical correct,
  `meta description` ramenée sous 155 caractères, 5 questions de FAQ, ≥ 4 sections.
  Un seul échec ⇒ aucun fichier n'est écrit et le workflow ne committe rien.
- **Règles éditoriales** de `BLOG_WORKFLOW.md` injectées dans le prompt : interdiction
  d'inventer prix, chiffres, noms, dates et réglementations ; seuls les faits listés
  dans `facts_allowed` (blog-config.json) sont autorisés.
- Aucun article ni aucune page existante n'est jamais modifié.

## 4. Coût estimé

Avec `gpt-4o-mini` : environ **1 500 jetons d'entrée** et **3 000 jetons de sortie**
par article.

| | Par article | Par an (52 lundis) |
|---|---|---|
| Coût OpenAI | **≈ 0,002 $** (~0,002 €) | **≈ 0,10 $** |
| Minutes GitHub Actions | ~1 min | ~52 min (gratuit sur dépôt public) |

Le coût est donc négligeable. Ordre de grandeur indicatif : vérifier les tarifs en
vigueur sur <https://openai.com/api/pricing/>.

À noter : la réserve de sujets couvre **12 semaines**. Passé ce délai, le workflow
sortira en code 78 chaque lundi sans rien publier — il suffit d'ajouter des lignes
au tableau §7 de `BLOG_WORKFLOW.md` pour relancer la production.

## 5. Relire avant que ça parte en ligne

Le workflow publie sans validation humaine. Deux réflexes utiles :

- s'abonner aux notifications d'échec du workflow (Actions → *Watch*) ;
- relire l'article du lundi dans la journée : le contenu est cadré par le prompt,
  mais un modèle reste un modèle. En cas de problème, `git revert` du commit
  `chore(blog): article auto du …` suffit à retirer l'article et à remettre
  `sitemap.xml`, `rss.xml` et la liste d'articles dans leur état antérieur.
