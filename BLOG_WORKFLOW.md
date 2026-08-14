# Workflow blog — Le Belvédère du Domaine

Site statique HTML/CSS hébergé sur GitHub Pages.
Domaine canonique : **https://belvedere65.com/** (HTTPS, sans `www`, cf. `CNAME`).

---

## 1. Structure

```
/
├─ index.html                          page d'accueil (une seule page, ancres #)
├─ mentions-legales.html
├─ assets/
│  └─ blog.css                         feuille de style du blog (tokens du site)
├─ blog/
│  ├─ index.html                       liste des articles
│  └─ <slug>/index.html                un article = un dossier + index.html
├─ sitemap.xml
├─ robots.txt
├─ rss.xml
└─ llms.txt
```

Un article vit dans son propre dossier pour obtenir une URL propre terminée par `/` :
`https://belvedere65.com/blog/<slug>/`

---

## 2. Publier un nouvel article — checklist

1. **Créer le dossier** `blog/<slug>/` et y copier le fichier `index.html` d'un
   article existant comme gabarit.
   Slug : minuscules, tirets, sans accents, 3 à 6 mots, mot-clé principal + ville
   ou département. Ex. `seminaire-entreprise-tarbes-hautes-pyrenees`.

2. **Head** — remplacer :
   - `<title>` (≤ 60 caractères)
   - `<meta name="description">` (**< 155 caractères**, une promesse concrète)
   - `<link rel="canonical">` → `https://belvedere65.com/blog/<slug>/`
   - Open Graph : `og:title`, `og:description`, `og:url`, `article:published_time`,
     `article:modified_time`, `article:section`
   - Twitter Card : `twitter:title`, `twitter:description`
   - Les balises `geo.*` et l'image OG restent inchangées.

3. **JSON-LD** — trois blocs à mettre à jour :
   - `Article` : `headline`, `description`, `datePublished`, `dateModified`,
     `mainEntityOfPage.@id`, `url`, `articleSection`
   - `BreadcrumbList` : position 3 = titre + URL de l'article
   - `FAQPage` : **5 questions**, dont le texte doit être **identique mot pour mot**
     à celui affiché dans le `<div class="faq">` du corps de page.

4. **Corps de l'article** — voir §3.

5. **Fil d'ariane HTML** : mettre à jour le dernier `<li>`.

6. **Référencer l'article** dans :
   - `blog/index.html` → nouvelle `<a class="post-card">` **en haut** de `.post-list`
     + entrée dans le tableau `blogPost` du JSON-LD `Blog`
   - `sitemap.xml` → nouveau bloc `<url>` (`priority` 0.7, `changefreq` monthly)
     et mise à jour du `<lastmod>` de `/blog/`
   - `rss.xml` → nouvel `<item>` **en haut** de la liste + `<lastBuildDate>`
     (format RFC-822 : `Sat, 15 Aug 2026 08:00:00 +0200`)
   - `llms.txt` → ligne dans « Pages principales »

7. **Vérifier** (§5) puis committer et pousser sur `main`.
   GitHub Pages publie automatiquement en une à deux minutes.

---

## 3. Règles de rédaction

- **Longueur** : 1 200 à 1 500 mots.
- **Structure** : un `<h1>` unique, 5 à 8 `<h2>`, des `<h3>` quand une section
  se subdivise. Un `<blockquote>` maximum pour respirer.
- **FAQ** : exactement 5 questions, en fin d'article, dans `<details>`, dupliquées
  en JSON-LD `FAQPage`.
- **Ancrage local systématique** : Tournay, Tarbes, Lannemezan,
  Bagnères-de-Bigorre, Lourdes, Hautes-Pyrénées (65), piémont pyrénéen, Occitanie.
  Le local doit servir l'argument, pas être plaqué en fin de phrase.
- **Ancrage métier** : lieu de réception, mariage, séminaire, réception privée,
  hébergement sur place, espaces modulables, plan B météo, capacité par configuration.
- **Ton** : sobre, précis, à la première personne du pluriel côté domaine.
  Pas de superlatifs creux, pas de ponctuation exclamative.
- **Un CTA** en fin d'article (`.cta-box`), jamais au milieu du texte.
- **Maillage interne** : au moins un lien vers `/#contact` ou une section de
  l'accueil, et un lien vers un autre article dès qu'il en existe deux.

### Interdictions strictes

Ne jamais inventer :

- des **prix**, forfaits, fourchettes tarifaires ou acomptes ;
- des **chiffres précis** non vérifiés (nombre de mariages par an, superficies,
  distances kilométriques, taux de remplissage, nombre de chambres) ;
- des **noms de clients**, témoignages ou citations ;
- des **réglementations**, obligations légales, normes ERP ou horaires d'arrêté ;
- des **dates** (fondation, rénovation, historique du lieu).

Faits validés et réutilisables tels quels : capacité **jusqu'à 200 personnes**,
hébergement sur place, parking privé, espaces modulables, espaces extérieurs,
vue panoramique sur les Pyrénées, visite virtuelle 360°.
Toute autre donnée doit être confirmée par le domaine avant publication.

En cas de doute : **formuler qualitativement** (« à courte distance de Tarbes »)
plutôt que quantitativement (« à 18 km de Tarbes »).

---

## 4. NAP — à ne jamais faire diverger

Ces informations doivent être strictement identiques partout (site, JSON-LD,
`llms.txt`, fiches externes, réseaux sociaux) :

| Champ      | Valeur                                    |
|------------|-------------------------------------------|
| Nom        | Le Belvédère du Domaine                   |
| Adresse    | 2 chemin de la Lande, 65190 Tournay       |
| Téléphone  | 06 37 21 04 00 — format lien `+33637210400` |
| E-mail     | belvederedu65@gmail.com                   |
| Site       | https://belvedere65.com/                  |
| GPS        | 43.1901 ; 0.2819                          |

Le type `EventVenue` (sous-type de `LocalBusiness`) est déjà déclaré en JSON-LD
sur la page d'accueil. **Ne pas ajouter de second bloc `LocalBusiness`** : un
seul suffit et deux blocs concurrents dégradent la lecture par les moteurs.

---

## 5. Vérifications avant push

- [ ] `<title>` unique et `<meta name="description">` sous 155 caractères
- [ ] `canonical` en `https://belvedere65.com/…` avec le `/` final
- [ ] Un seul `<h1>` sur la page
- [ ] Les 3 blocs JSON-LD sont du JSON valide
      (`python3 -c "import json,sys;json.load(sys.stdin)"`)
- [ ] Les 5 questions FAQ sont identiques entre HTML et JSON-LD
- [ ] L'article est listé dans `blog/index.html`, `sitemap.xml`, `rss.xml`, `llms.txt`
- [ ] Le lien **Blog** est présent dans le pied de page de toutes les pages
- [ ] Aucun chemin relatif cassé : dans `/blog/`, tous les liens vers la racine
      commencent par `/` (`/assets/blog.css`, `/imagesdomaine/…`, `/#contact`)
- [ ] Test visuel mobile (< 560 px) et desktop

Validateurs utiles : Rich Results Test (Google), validator.w3.org/feed pour le RSS.

---

## 6. Rythme et priorités

Un article par mois suffit pour un site de cette taille. Mieux vaut quatre
articles solides par an que douze pages minces. Priorité aux sujets qui répondent
à une question réellement posée par les couples et les entreprises lors des
visites — ce sont ceux qui ressortent dans les moteurs de réponse.

---

## 7. Douze sujets à traiter

| # | Sujet | Angle / intention | Cible |
|---|-------|-------------------|-------|
| 1 | Se marier dans les Hautes-Pyrénées : comment choisir son domaine de réception | Critères de choix d'un lieu — **publié** | Mariage |
| 2 | Organiser un séminaire d'entreprise en Hautes-Pyrénées : le guide pratique | Format, journée type, ce qu'attendent les participants hors des salles urbaines | Séminaire |
| 3 | Mariage en extérieur au pied des Pyrénées : construire un plan B qui tient | Repli météo, cérémonie laïque, confort des invités selon la saison | Mariage |
| 4 | Loger ses invités le soir du mariage en zone rurale | Hébergement sur place, navettes, brunch du lendemain, sécurité des retours | Mariage |
| 5 | Se marier à Tarbes et ses environs : quel lieu pour quel format | Comparaison des formats (salle, domaine, extérieur) sur le bassin tarbais | Mariage / local |
| 6 | Rétroplanning de mariage : les 12 étapes, du lieu au jour J | Guide chronologique, sans dates ni budgets chiffrés | Mariage |
| 7 | Un team building en montagne : ce qui fonctionne vraiment | Activités de piémont pyrénéen, articulation avec une journée de travail | Séminaire |
| 8 | Quelle saison pour se marier dans les Pyrénées ? | Lumière, températures, disponibilités, avantages de l'avant et l'arrière-saison | Mariage |
| 9 | Anniversaire, baptême, fête de famille : réussir une réception privée | Format intimiste, modularité des espaces, rythme de la journée | Réception privée |
| 10 | Les questions à poser lors d'une visite de lieu de réception | Checklist imprimable, ce que les couples oublient de demander | Mariage / conversion |
| 11 | Photos de mariage face aux Pyrénées : les moments et les lieux à ne pas manquer | Collaboration avec le photographe, heures de lumière, repérages sur le domaine | Mariage |
| 12 | Que faire autour de Tournay pendant un week-end de mariage ? | Occuper les invités entre l'arrivée et le brunch, ancrage territorial fort | Local / séjour |

Sujets 2, 3 et 10 en priorité : forte intention de recherche et complémentarité
directe avec l'article déjà en ligne.
