#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génération automatique d'un article de blog — Le Belvédère du Domaine.

Le script :
  1. lit blog-config.json ;
  2. extrait de BLOG_WORKFLOW.md le tableau des sujets suggérés et les règles
     éditoriales ;
  3. scanne /blog/*/index.html pour savoir quels sujets sont déjà traités ;
  4. choisit le prochain sujet non traité (ordre séquentiel) ;
  5. relit l'article de référence pour s'en servir de gabarit HTML ;
  6. demande à l'API OpenAI le seul CONTENU éditorial, en JSON structuré
     (titre, chapô, sections h2/h3, paragraphes, listes, FAQ) ;
  7. valide ce contenu, puis ASSEMBLE lui-même la page : head, meta, canonical,
     Open Graph, Twitter Card, les trois blocs JSON-LD, le fil d'Ariane, le
     marqueur d'idempotence, le header et le footer viennent du gabarit et du
     script — jamais du modèle ;
  8. écrit /blog/<slug>/index.html, puis met à jour blog/index.html,
     sitemap.xml, rss.xml et llms.txt.

Le modèle n'écrit donc plus une ligne de HTML. Auparavant il régénérait toute la
page : les deux tiers de ses tokens de sortie partaient en balisage, ce qui
plafonnait le corps rédigé autour de 400 mots quelle que soit la consigne.

Codes de sortie :
   0  succès
   1  erreur (rien n'a été écrit)
  78  aucun nouveau sujet à traiter (EX_CONFIG — arrêt propre)

Options :
  --dry-run       n'écrit aucun fichier, affiche le résultat
  --mock          n'appelle pas l'API (contenu de démonstration)
  --rewrite SLUG  régénère un article existant et écrase son fichier
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "blog-config.json"
WORKFLOW_PATH = ROOT / "BLOG_WORKFLOW.md"
BLOG_DIR = ROOT / "blog"
BLOG_INDEX = BLOG_DIR / "index.html"
SITEMAP = ROOT / "sitemap.xml"
RSS = ROOT / "rss.xml"
LLMS = ROOT / "llms.txt"

EXIT_OK, EXIT_ERROR, EXIT_NOTHING_TODO = 0, 1, 78

# Volume du corps rédigé, FAQ exclue, compté sur le contenu et non sur le HTML.
#  · PROMPT_MIN/MAX_WORDS : la cible, annoncée au modèle et seuil de rattrapage.
#  · MIN/MAX_WORDS        : bornes de validation, plus larges (tolérance ±30 %).
MIN_WORDS, MAX_WORDS = 900, 1900
PROMPT_MIN_WORDS, PROMPT_MAX_WORDS = 1200, 1500

# Nombre maximal d'appels OpenAI pour un article, rattrapages compris.
MAX_CALLS = 3

# Réapprovisionnement automatique de la réserve de sujets.
#  · TOPIC_RESERVE_MIN : en dessous de ce nombre de sujets non traités, on
#    regarnit la liste. 8 laisse deux mois d'avance : la panne sèche du lundi
#    matin n'arrive jamais.
#  · TOPIC_BATCH       : taille du lot demandé au modèle.
#  · TOPIC_MAX_CALLS   : plafond d'appels pour un réapprovisionnement.
#  · TOPICS_MODEL      : modèle dédié aux sujets, indépendant de celui qui rédige.
TOPIC_RESERVE_MIN = 8
TOPIC_BATCH = 40
TOPIC_MAX_CALLS = 2
TOPICS_MODEL = "gpt-4o"

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
DAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Mots vides écartés de la construction des slugs.
STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "et", "ou", "a", "au",
    "aux", "en", "dans", "sur", "pour", "par", "avec", "sans", "que", "qui", "quoi",
    "ce", "cet", "cette", "ces", "se", "sa", "son", "ses", "nos", "notre", "votre",
    "vos", "est", "ne", "pas", "plus", "tout", "tous", "toute", "toutes", "y", "il",
    "elle", "on", "vraiment", "bien", "quel", "quelle", "comment", "faut",
}


# ─────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[blog] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[blog][ERREUR] {msg}", file=sys.stderr, flush=True)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


def slugify(title: str, max_words: int = 7) -> str:
    """Slug déterministe : même titre => même slug (garantit l'idempotence)."""
    text = strip_accents(title.lower())
    text = text.replace("'", " ").replace("’", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if w and w not in STOPWORDS]
    if not words:
        words = [w for w in text.split() if w]
    return "-".join(words[:max_words])


def esc(text: str) -> str:
    """Échappement HTML. Tout le contenu du modèle passe par là : il fournit du
    texte brut, jamais du markup, ce qui rend une injection HTML impossible."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def inline(text: str) -> str:
    """Rend le balisage inline autorisé dans le texte du modèle, après
    échappement : **gras** et [libellé](/chemin-interne).

    Les liens sont restreints aux chemins commençant par « / » : le maillage
    interne reste possible, un lien externe devient structurellement impossible."""
    out = esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\((/[^)\s]*)\)", r'<a href="\2">\1</a>', out)
    return out


def plain(text: str) -> str:
    """Texte débarrassé du balisage inline — pour les JSON-LD et les meta."""
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"\[([^\]]+)\]\((/[^)\s]*)\)", r"\1", out)


def content_word_count(data: dict) -> int:
    """Volume rédactionnel du corps, FAQ exclue — compté sur le contenu lui-même
    et non sur du HTML : plus de balises ni de boilerplate dans le total."""
    words = len(plain(data.get("lede", "")).split())
    for section in data.get("sections", []):
        words += len(plain(section.get("h2", "")).split())
        for block in section.get("content", []):
            words += len(plain(block.get("text", "")).split())
            for item in block.get("items", []) or []:
                words += len(plain(item).split())
    return words


def fr_date(d: dt.date) -> str:
    return f"{d.day} {MONTHS_FR[d.month - 1]} {d.year}"


def rfc822(d: dt.date, hour: str = "09:00:00") -> str:
    return f"{DAYS_EN[d.weekday()]}, {d.day:02d} {MONTHS_EN[d.month - 1]} {d.year} {hour} +0200"


# ─────────────────────────────────────────────────────────────
# Lecture de la configuration et du workflow
# ─────────────────────────────────────────────────────────────

REQUIRED_KEYS = (
    "site_name", "site_url", "sector", "location", "geo_keywords", "tone",
    "author", "target_word_count", "faq_questions_count", "language", "model",
    "temperature", "topic_marker_prefix", "og_image", "logo_path",
    "default_article_section", "internal_link_targets", "reference_article_slug",
    "facts",
)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Configuration introuvable : {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_KEYS if cfg.get(k) in (None, "", [], {})]
    if missing:
        raise ValueError("Clé(s) manquante(s) ou vide(s) dans blog-config.json : "
                         + ", ".join(missing))
    cfg["site_url"] = cfg["site_url"].rstrip("/")
    return cfg


def parse_topics(workflow: str) -> list[dict]:
    """Extrait les sujets du tableau « Douze sujets à traiter » de
    BLOG_WORKFLOW.md.

    Convention locale : les sujets sont présentés dans un tableau Markdown
    « | n° | Sujet | Angle / intention | Cible | » et non dans une liste
    numérotée. Un sujet déjà publié à la main est signalé par « publié » dans
    la colonne Angle — les articles générés, eux, portent un marqueur HTML.
    """
    topics: list[dict] = []
    for line in workflow.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or not re.fullmatch(r"\d+", cells[0]):
            continue

        published = bool(re.search(r"publi[ée]s?\b", cells[2], re.I))
        brief = re.sub(r"\*\*(.*?)\*\*", r"\1", cells[2]).strip()
        brief = re.sub(r"\s*[—–-]\s*publi[ée]s?\s*$", "", brief, flags=re.I).strip()

        topics.append({
            "num": int(cells[0]),
            "title": re.sub(r"\*\*(.*?)\*\*", r"\1", cells[1]).strip(),
            "brief": brief,
            "audience": cells[3].strip(),
            "declared_slug": None,
            "declared_published": published,
        })

    if not topics:
        raise ValueError("Aucun sujet exploitable trouvé dans BLOG_WORKFLOW.md "
                         "(tableau des sujets suggérés absent ou modifié)")
    topics.sort(key=lambda t: t["num"])
    return topics


def parse_editorial_rules(workflow: str) -> str:
    """Récupère les règles de rédaction du blog pour les injecter dans le prompt.

    Convention locale : la section s'intitule « Règles de rédaction » et englobe
    la sous-section « Interdictions strictes », qui porte l'essentiel des
    garde-fous. On prend donc tout jusqu'au titre de niveau 2 suivant.
    """
    m = re.search(r"^##\s+\d+\.\s+Règles de rédaction.*?$(.*?)(?=^##\s)",
                  workflow, flags=re.M | re.S)
    if not m:
        m = re.search(r"^##\s+\d+\.\s+Règles éditoriales.*?$(.*?)(?=^##\s)",
                      workflow, flags=re.M | re.S)
    return m.group(1).strip() if m else ""


# ─────────────────────────────────────────────────────────────
# État du blog
# ─────────────────────────────────────────────────────────────

def scan_blog(marker_prefix: str) -> tuple[set[int], set[str]]:
    """Retourne (numéros de sujets déjà traités, slugs existants)."""
    done_nums: set[int] = set()
    slugs: set[str] = set()
    if not BLOG_DIR.exists():
        return done_nums, slugs
    for path in sorted(BLOG_DIR.glob("*/index.html")):
        slug = path.parent.name
        slugs.add(slug)
        html = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(rf"<!--\s*{re.escape(marker_prefix)}:\s*(\d+)\s*-->", html)
        if m:
            done_nums.add(int(m.group(1)))
    return done_nums, slugs


def topic_covered_by_slug(topic: dict, slugs: set[str]) -> bool:
    """Filet de sécurité : un article existant couvre-t-il déjà ce sujet ?
    Utile pour les articles antérieurs au marqueur d'idempotence."""
    candidate = {w for w in slugify(topic["title"], max_words=12).split("-") if len(w) > 4}
    if not candidate:
        return False
    for slug in slugs:
        words = {w for w in slug.split("-") if len(w) > 4}
        if words and len(candidate & words) / len(candidate) >= 0.6:
            return True
    return False


def topic_is_pending(topic: dict, done_nums: set[int], slugs: set[str],
                     verbose: bool = False) -> bool:
    """Définition UNIQUE de « sujet non traité ».

    Le comptage de la réserve et le choix du sujet s'appuient tous deux sur
    cette fonction : ils ne peuvent donc pas diverger — sans quoi on pourrait
    compter huit sujets disponibles puis n'en trouver aucun à rédiger.
    """
    if topic["num"] in done_nums:
        return False
    if topic["declared_published"]:
        if verbose:
            log(f"Sujet n°{topic['num']} ignoré : marqué « publié » dans BLOG_WORKFLOW.md.")
        return False
    if topic["declared_slug"] and topic["declared_slug"] in slugs:
        return False
    if slugify(topic["title"]) in slugs:
        # Le dossier existe déjà : on considère le sujet traité (idempotence).
        return False
    if topic_covered_by_slug(topic, slugs):
        if verbose:
            log(f"Sujet n°{topic['num']} ignoré : un article proche existe déjà.")
        return False
    return True


def pending_topics(topics: list[dict], done_nums: set[int], slugs: set[str],
                   verbose: bool = False) -> list[dict]:
    """Les sujets encore à traiter, dans l'ordre de la liste."""
    return [t for t in topics if topic_is_pending(t, done_nums, slugs, verbose)]


def pick_topic(topics: list[dict], done_nums: set[int], slugs: set[str]) -> dict | None:
    """Premier sujet non traité, dans l'ordre de la liste."""
    pending = pending_topics(topics, done_nums, slugs, verbose=True)
    if not pending:
        return None
    topic = pending[0]
    topic["slug"] = slugify(topic["title"])
    return topic


# ─────────────────────────────────────────────────────────────
# Réapprovisionnement de la réserve de sujets
# ─────────────────────────────────────────────────────────────

def clean_line(text: str) -> str:
    """Normalise un champ destiné à une cellule de tableau Markdown.

    Une barre verticale ou un retour à la ligne dans une cellule casserait le
    tableau — et donc parse_topics() au run suivant. On les neutralise ici.
    """
    out = str(text or "").replace("|", "/").replace("\r", " ")
    out = out.replace("\n", " ").replace("`", "").replace("**", "")
    out = re.sub(r"[ \t]+", " ", out).strip()
    return out.strip(" -–—")


def parse_topics_response(raw: dict) -> list[dict]:
    """Extrait la liste de sujets de la réponse du modèle."""
    if not isinstance(raw, dict):
        raise ValueError("réponse de génération de sujets inexploitable")
    items = raw.get("topics") or raw.get("sujets") or []
    if not isinstance(items, list):
        raise ValueError("champ « topics » absent ou mal formé")

    out: list[dict] = []
    for item in items:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = clean_line(item.get("title") or item.get("titre") or "")
        if len(title) < 15:
            continue
        out.append({
            "title": title,
            "angle": clean_line(item.get("angle") or item.get("intention") or ""),
            "audience": clean_line(item.get("audience") or item.get("cible") or ""),
        })
    return out


def dedupe_topics(candidates: list[dict], known_slugs: set[str]) -> list[dict]:
    """Filtre les doublons.

    La déduplication porte sur le SLUG, pas sur le titre : c'est le slug qui
    sert de clé d'idempotence (nom du dossier de l'article), donc deux titres
    différents qui produisent le même slug sont bel et bien un doublon.
    """
    seen = set(known_slugs)
    kept: list[dict] = []
    for topic in candidates:
        slug = slugify(topic["title"])
        if not slug or slug in seen:
            continue
        seen.add(slug)
        topic["slug"] = slug
        kept.append(topic)
    return kept


def build_topics_prompt(cfg: dict, existing_titles: list[str],
                        count: int) -> tuple[str, str]:
    """Prompt de génération de sujets, ancré sur l'activité et le territoire."""
    system = f"""Tu es consultant SEO local pour une entreprise française.
Tu proposes des sujets d'articles de blog, concrets et actionnables.

Tu réponds UNIQUEMENT par un objet JSON valide, sans bloc de code markdown :

{{
  "topics": [
    {{"title": "titre du sujet, une question ou une promesse claire",
      "angle": "l'angle en une phrase : ce que l'article traite concrètement",
      "audience": "la cible en 1 à 3 mots"}}
  ]
}}

RÈGLES
- Exactement {count} sujets, tous différents les uns des autres.
- Chaque sujet doit répondre à une question que se pose réellement un client
  avant de réserver, ou juste après. Rien de théorique, rien de promotionnel.
- Ancrage local obligatoire dans une bonne partie des sujets : le territoire
  doit servir l'angle, pas décorer le titre.
- Varie les intentions : choix et comparaison, préparation et organisation,
  logistique, saisonnalité, budget (sans jamais donner de chiffre), et
  découverte du territoire.
- Varie aussi les cibles selon les prestations de l'entreprise.
- Titres de 40 à 95 caractères, sans nom de marque, sans point d'exclamation,
  sans superlatif creux, sans « guide ultime » ni « tout savoir sur ».
- N'inclus aucun prix, chiffre, date, nom de client ni réglementation.
- Ne reprends aucun sujet déjà listé ci-dessous, ni une simple reformulation.
"""

    listing = "\n".join(f"- {t}" for t in existing_titles) or "- aucun"
    user = f"""Entreprise : {cfg['site_name']} — {cfg['sector']}.
Zone : {cfg['location']}.

Mots-clés géographiques du secteur :
{', '.join(cfg['geo_keywords'])}.

Prestations et faits de référence :
{chr(10).join('- ' + f for f in cfg.get('facts', []))}

SUJETS DÉJÀ LISTÉS — à ne pas reproduire, même reformulés :
{listing}

Réponds par le seul objet JSON contenant {count} sujets inédits."""
    return system, user


def mock_topics(cfg: dict, count: int) -> list[dict]:
    """Sujets de démonstration pour les tests : aucun appel API."""
    city = cfg.get("nap", {}).get("city") or cfg["location"].split(",")[0].strip()
    gabarits = [
        "Préparer {}  dans les Hautes-Pyrénées : les étapes à ne pas sauter",
        "Choisir {} autour de {} : les critères qui comptent",
        "{} en arrière-saison : ce que cela change concrètement",
        "Organiser {} : la logistique vue par les invités",
        "{} et météo de montagne : anticiper sans stresser",
    ]
    themes = ["un vin d'honneur", "une cérémonie laïque", "un brunch du lendemain",
              "une soirée dansante", "un séminaire résidentiel", "un cocktail dînatoire",
              "une réception d'anniversaire", "un team building"]
    out = []
    for i in range(count):
        gabarit = gabarits[i % len(gabarits)]
        theme = themes[(i // len(gabarits)) % len(themes)]
        title = gabarit.format(theme, city) if "{}" in gabarit[gabarit.find("{}") + 2:] \
            else gabarit.format(theme)
        out.append({
            "title": f"{title} (variante {i + 1})",
            "angle": "Sujet de démonstration produit sans appel API.",
            "audience": "Démonstration",
        })
    return out


def generate_topics(cfg: dict, existing_titles: list[str], known_slugs: set[str],
                    count: int, mock: bool = False) -> list[dict]:
    """Produit un lot de sujets inédits, dans la limite de TOPIC_MAX_CALLS appels."""
    if mock:
        return dedupe_topics(mock_topics(cfg, count), known_slugs)

    kept: list[dict] = []
    seen = set(known_slugs)
    titles = list(existing_titles)

    for attempt in range(1, TOPIC_MAX_CALLS + 1):
        missing = count - len(kept)
        if missing <= 0:
            break
        system, user = build_topics_prompt(cfg, titles, missing)
        log(f"Génération de sujets — tentative {attempt}/{TOPIC_MAX_CALLS} "
            f"({missing} sujets demandés).")
        raw = generate_content(cfg, system, user, model=TOPICS_MODEL)
        fresh = dedupe_topics(parse_topics_response(raw), seen)
        log(f"  {len(fresh)} sujet(s) inédit(s) retenu(s) après déduplication.")
        if not fresh:
            break
        for topic in fresh:
            seen.add(topic["slug"])
            titles.append(topic["title"])
        kept.extend(fresh)

    return kept[:count]


def append_topics_to_workflow(new_topics: list[dict], section: str) -> int:
    r"""Ajoute les sujets à la fin du TABLEAU de BLOG_WORKFLOW.md.

    Le tableau n'est pas en fin de fichier — un paragraphe le suit — donc on
    insère après la dernière ligne numérotée, pas après la dernière ligne du
    document. La numérotation reprend au dernier numéro + 1.

    Les motifs de fin de ligne utilisent [ \t]*$ et non \s*$ : en mode
    multiligne, \s engloberait le retour à la ligne et la réécriture
    souderait deux lignes du tableau, ce qui le casserait.
    """
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    row = re.compile(r"^\|[ \t]*(\d+)[ \t]*\|")
    last_idx, last_num = -1, 0
    for i, line in enumerate(lines):
        m = row.match(line)
        if m:
            last_idx = i
            last_num = max(last_num, int(m.group(1)))
    if last_idx == -1:
        raise ValueError("Tableau des sujets introuvable dans BLOG_WORKFLOW.md")

    # La dernière ligne du tableau se termine-t-elle par un retour à la ligne ?
    tail = "" if lines[last_idx].endswith("\n") else "\n"

    rows = []
    for offset, topic in enumerate(new_topics, start=1):
        rows.append(f"| {last_num + offset} | {clean_line(topic['title'])} "
                    f"| {clean_line(topic['angle']) or 'À développer'} "
                    f"| {clean_line(topic['audience']) or section} |\n")

    lines[last_idx] = lines[last_idx] + tail
    lines[last_idx + 1:last_idx + 1] = rows
    out = "".join(lines)

    # Le titre annonçait un nombre figé de sujets : il ne peut plus être exact.
    out = re.sub(r"^(##[ \t]+\d+\.[ \t]+)Douze sujets à traiter[ \t]*$",
                 r"\1Sujets à traiter", out, count=1, flags=re.M)

    WORKFLOW_PATH.write_text(out, encoding="utf-8")
    return len(rows)


def git_commit_file(path: Path, message: str) -> bool:
    """Committe un seul fichier. Renvoie False si rien n'était à committer."""
    rel = str(path.relative_to(ROOT))
    status = subprocess.run(["git", "status", "--porcelain", "--", rel],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    if not status.stdout.strip():
        log(f"Rien à committer pour {rel}.")
        return False
    subprocess.run(["git", "add", "--", rel], cwd=ROOT, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", message, "--", rel], cwd=ROOT,
                   check=True, capture_output=True, text=True)
    log(f"Commit : {message}")
    return True


def replenish_topics(cfg: dict, topics: list[dict], done_nums: set[int],
                     slugs: set[str], mock: bool = False,
                     dry_run: bool = False) -> int:
    """Regarnit la liste de sujets si la réserve passe sous le seuil.

    Renvoie le nombre de sujets ajoutés. Appelée AVANT la rédaction : même
    partie de zéro, le run du jour publie quand même un article.
    """
    reserve = len(pending_topics(topics, done_nums, slugs))
    log(f"Réserve de sujets non traités : {reserve} (seuil {TOPIC_RESERVE_MIN}).")
    if reserve >= TOPIC_RESERVE_MIN:
        log("Réserve suffisante — aucun réapprovisionnement.")
        return 0

    known = set(slugs) | {slugify(t["title"]) for t in topics}
    fresh = generate_topics(cfg, [t["title"] for t in topics], known,
                            TOPIC_BATCH, mock=mock)
    if not fresh:
        log("Aucun sujet inédit obtenu — réserve inchangée.")
        return 0

    if dry_run:
        log(f"DRY-RUN : {len(fresh)} sujet(s) auraient été ajoutés. Exemples :")
        for topic in fresh[:5]:
            log(f"  · {topic['title']}")
        return 0

    added = append_topics_to_workflow(fresh, cfg["default_article_section"])
    log(f"{added} sujet(s) ajouté(s) à {WORKFLOW_PATH.name}.")
    git_commit_file(WORKFLOW_PATH, f"chore(blog): {added} nouveaux sujets")
    return added


def load_reference_article(cfg: dict, slugs: set[str]) -> tuple[str, str]:
    """Relit un article existant : il sert de gabarit (jamais de template en dur)."""
    preferred = cfg.get("reference_article_slug")
    candidates = [preferred] if preferred in slugs else []
    candidates += sorted(s for s in slugs if s != preferred)
    for slug in candidates:
        path = BLOG_DIR / slug / "index.html"
        if path.exists():
            return slug, path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "Aucun article de référence dans /blog/ : impossible de déduire le gabarit.")


# ─────────────────────────────────────────────────────────────
# Rédaction : le modèle ne produit QUE du contenu éditorial
# ─────────────────────────────────────────────────────────────

def volume_rank(errors: list[str], wc: int) -> tuple[int, int]:
    """Clé de comparaison entre deux copies : celle qui a le moins d'erreurs
    prime, puis on préfère celle qui approche le mieux la cible."""
    deficit = max(0, PROMPT_MIN_WORDS - wc)
    excess = max(0, wc - MAX_WORDS)
    return (len(errors), deficit + excess)


def build_correction(cfg: dict, errors: list[str], wc: int) -> str:
    """Message de reprise adressé au modèle. Il ne porte pas seulement sur le
    volume : toute erreur de validation que le modèle peut corriger lui-même
    (maillage interne, nombre de questions, longueur du title) y passe, tant
    qu'il reste des appels au budget."""
    demands = []
    if wc < PROMPT_MIN_WORDS:
        demands.append(
            f"Tu as généré {wc} mots pour le corps (FAQ exclue), il en faut au moins "
            f"{PROMPT_MIN_WORDS}. Développe chaque section : ajoute des paragraphes, "
            "des exemples concrets, du contexte local, des nuances. Ne retire aucune "
            "section.")
    elif wc > MAX_WORDS:
        demands.append(
            f"Tu as généré {wc} mots pour le corps (FAQ exclue), c'est trop : il en "
            f"faut au plus {PROMPT_MAX_WORDS}. Resserre chaque section sans en "
            "supprimer aucune.")

    if any("maillage" in e for e in errors):
        targets = "\n".join(f"  {t}" for t in cfg["internal_link_targets"])
        demands.append(
            "Il manque des liens internes, c'est rédhibitoire. Insère dans le corps "
            "au moins DEUX liens markdown vers ces chemins exacts, placés dans deux "
            f"sections différentes :\n{targets}\net au moins UN lien vers /blog/. "
            f"Écris-les sous la forme [libellé descriptif]({cfg['internal_link_targets'][0]}), "
            "en recopiant le chemin tel quel. Ne touche à rien d'autre.")

    others = [e for e in errors if "maillage" not in e and "volume" not in e]
    if others:
        demands.append("Corrige aussi ces points : " + " ; ".join(others) + ".")

    if not demands:
        demands.append("Reprends ton JSON en respectant toutes les consignes.")
    return " ".join(demands) + " Réponds par le seul objet JSON complet."


def build_prompt(cfg: dict, topic: dict, rules: str) -> tuple[str, str]:
    """Prompt court : plus de gabarit HTML à recopier, plus de contraintes de
    balisage. Le modèle écrit, le script fabrique la page."""
    targets = cfg["internal_link_targets"]
    targets_bullets = "\n".join(f"    {t}" for t in targets)

    system = f"""Tu es rédacteur SEO/GEO senior pour une entreprise locale française.
Tu écris du CONTENU, jamais du HTML : la mise en page est faite par ailleurs.

Tu réponds UNIQUEMENT par un objet JSON valide, sans bloc de code markdown,
respectant exactement ce schéma :

{{
  "title": "titre de la page, 55 à 60 caractères, sans le nom du site",
  "h1": "titre affiché en haut de l'article, court et percutant",
  "breadcrumb": "libellé court pour le fil d'Ariane (2 à 4 mots)",
  "meta_description": "résumé de moins de 155 caractères",
  "lede": "chapô d'introduction, 60 à 90 mots, qui plante une situation concrète",
  "sections": [
    {{"h2": "titre de section",
      "content": [
        {{"type": "p", "text": "paragraphe"}},
        {{"type": "h3", "text": "sous-titre"}},
        {{"type": "ul", "items": ["élément", "élément"]}},
        {{"type": "ol", "items": ["étape", "étape"]}}
      ]}}
  ],
  "faq": [{{"question": "…", "answer": "…"}}]
}}

RÈGLES DE CONTENU
- Volume : le corps (lede + sections, FAQ exclue) fait entre {PROMPT_MIN_WORDS} et
  {PROMPT_MAX_WORDS} mots. Compte les mots avant de répondre. C'est la contrainte
  la plus importante : en dessous de {PROMPT_MIN_WORDS} mots, la réponse est rejetée.
- Vise 5 à 7 sections « h2 », chacune avec 3 à 5 paragraphes nourris. Un paragraphe
  fait 60 à 110 mots : développe, donne des exemples concrets, du contexte local,
  des nuances. Ne fais jamais de paragraphe d'une seule phrase.
- FAQ : exactement {{faq_count}} questions, avec des réponses de 40 à 70 mots.
  Elles ne comptent pas dans le volume du corps.
- Balisage inline autorisé dans les textes, et lui seul :
  **gras** et [libellé](/chemin). Les liens sont forcément internes.
- Maillage interne — OBLIGATOIRE, la réponse est rejetée sans cela :
  place AU MOINS DEUX liens markdown vers ces chemins exacts, dans deux
  sections différentes du corps :
{targets_bullets}
  et AU MOINS UN lien vers /blog/.
  Forme attendue, à recopier telle quelle : [libellé descriptif]({targets[0]})
  Recopie les chemins sans les modifier, sans domaine et sans rien y ajouter.
- Ancres de liens : les libellés des liens internes doivent être descriptifs et
  se lire naturellement dans la phrase. Interdit : les libellés secs d'un seul
  mot comme « ici », « blog », « contact », « espaces ».
- Ton : sobre et précis. Pas de point d'exclamation, pas de superlatif creux,
  pas de « n'hésitez pas ». Parle de l'établissement à la première personne du
  pluriel, avec parcimonie.

GARDE-FOUS — NON NÉGOCIABLES
N'invente AUCUN prix, tarif, forfait ou acompte ; AUCUN chiffre précis non fourni
(superficies, distances en kilomètres, nombre de chambres, nombre d'événements par
an, taux de remplissage) ; AUCUN nom de client, témoignage ou avis ; AUCUNE date de
fondation ni élément d'historique ; AUCUNE réglementation, norme ERP, obligation
légale ou horaire d'arrêté ; AUCUNE adresse autre que celle fournie ci-dessous.
Si une information te manque, reformule pour t'en passer, ou formule
qualitativement (« à courte distance de Tarbes ») plutôt que chiffré.

FAITS AUTORISÉS (seule source de faits chiffrés, d'adresses et de coordonnées)
{{facts}}
""".replace("{faq_count}", str(cfg["faq_questions_count"])).replace(
        "{facts}", "\n".join(f"- {f}" for f in cfg.get("facts", [])))

    user = f"""Sujet n°{topic['num']} : {topic['title']}
Angle : {topic['brief'] or "à développer librement dans le cadre des règles"}
Cible : {topic.get('audience') or "clients du domaine"}

Entreprise : {cfg['site_name']} — {cfg['sector']}.
Zone : {cfg['location']}.
Ton : {cfg['tone']}. Langue : français.

Mots-clés géographiques à faire vivre naturellement (pas de bourrage) :
{', '.join(cfg['geo_keywords'])}.

RÈGLES ÉDITORIALES DU BLOG
{rules}

Réponds par le seul objet JSON."""

    return system, user


def generate_content(cfg: dict, system: str, user: str,
                     followup: list[dict] | None = None,
                     model: str | None = None) -> dict:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Le paquet 'openai' n'est pas installé (pip install openai).") from exc

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Variable d'environnement OPENAI_API_KEY absente.")

    client = OpenAI()
    # `model` permet à la génération de sujets d'utiliser son propre modèle sans
    # toucher à celui qui rédige les articles.
    model = model or cfg["model"]
    log(f"Appel OpenAI (modèle {model}, temperature {cfg['temperature']})…")
    response = client.chat.completions.create(
        model=model,
        temperature=cfg["temperature"],
        max_tokens=9000,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            *(followup or []),
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    if usage:
        log(f"Tokens : {usage.prompt_tokens} entrée + "
            f"{usage.completion_tokens} sortie = {usage.total_tokens}")
    if not content:
        raise ValueError("réponse vide")
    return json.loads(content)


def mock_content(cfg: dict, topic: dict) -> dict:
    """Contenu de démonstration pour --mock : même forme que la sortie du modèle,
    calibré pour dépasser la cible de volume."""
    filler = ("Dans les Hautes-Pyrénées, la question se pose différemment selon la "
              "saison et le format retenu pour la journée. Entre Tarbes, Tournay et "
              "l'entrée des vallées, les distances restent courtes, ce qui change "
              "beaucoup de choses dans la manière d'organiser une réception. Les "
              "attentes des uns et des autres varient, et c'est précisément pour cela "
              "qu'il vaut la peine de détailler chaque cas de figure plutôt que de "
              "donner une réponse unique qui ne conviendrait qu'à une minorité des "
              "situations que nous rencontrons réellement sur le domaine.")
    targets = cfg["internal_link_targets"]
    sections = []
    for i in range(7):          # 7 sections : le mock dépasse la cible de 1200
        content = [{"type": "p", "text": filler}, {"type": "p", "text": filler}]
        if i == 0:
            content.insert(1, {"type": "h3", "text": "Un point de départ concret"})
            content.append({"type": "p",
                            "text": f"Le détail figure sur [la présentation des espaces]"
                                    f"({targets[0]}) et sur [notre page de contact]"
                                    f"({targets[-1]})."})
        if i == 1:
            content.append({"type": "ul", "items": ["Premier repère utile",
                                                    "Deuxième repère utile",
                                                    "Troisième repère utile"]})
        if i == 2:
            content.append({"type": "p",
                            "text": "D'autres articles sont réunis dans "
                                    "[le journal du domaine](/blog/)."})
        sections.append({"h2": f"Section de démonstration n°{i + 1}", "content": content})
    return {
        "title": f"{topic['title'][:50]} | démo",
        "h1": topic["title"],
        "breadcrumb": topic["title"][:28],
        "meta_description": f"{topic['title'][:110]} — contenu de démonstration.",
        "lede": filler,
        "sections": sections,
        "faq": [{"question": f"Question de démonstration n°{i + 1} ?",
                 "answer": filler[:220]} for i in range(cfg["faq_questions_count"])],
    }


# ─────────────────────────────────────────────────────────────
# Validation du contenu
# ─────────────────────────────────────────────────────────────

CONTENT_TYPES = {"p", "h3", "ul", "ol", "strong"}


def validate_content(data: dict, cfg: dict) -> list[str]:
    """Contrôles bloquants sur le CONTENU. Tout ce que le script fabrique
    lui-même (canonical, OG, JSON-LD, marqueur, fil d'Ariane, structure) ne peut
    plus être erroné et n'est donc plus contrôlé ici."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["la réponse n'est pas un objet JSON"]

    for key in ("title", "h1", "breadcrumb", "meta_description", "lede"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f"champ « {key} » absent ou vide")

    title = data.get("title", "")
    if isinstance(title, str) and not 40 <= len(title) <= 70:
        errors.append(f"title hors bornes : {len(title)} caractères (attendu 40–70)")

    desc = data.get("meta_description", "")
    if isinstance(desc, str) and len(desc) >= 155:
        errors.append(f"meta description trop longue ({len(desc)} caractères)")

    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("aucune section")
    else:
        for i, section in enumerate(sections, 1):
            if not isinstance(section, dict) or not section.get("h2"):
                errors.append(f"section n°{i} sans titre h2")
                continue
            blocks = section.get("content")
            if not isinstance(blocks, list) or not blocks:
                errors.append(f"section n°{i} sans contenu")
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    errors.append(f"section n°{i} : bloc de contenu invalide")
                    continue
                kind = block.get("type")
                if kind not in CONTENT_TYPES:
                    errors.append(f"section n°{i} : type de bloc inconnu ({kind!r})")
                elif kind in ("ul", "ol"):
                    items = block.get("items") or block.get("text")
                    if not items:
                        errors.append(f"section n°{i} : liste {kind} vide")
                elif not block.get("text"):
                    errors.append(f"section n°{i} : bloc {kind} sans texte")

    faq = data.get("faq")
    if not isinstance(faq, list) or len(faq) != cfg["faq_questions_count"]:
        errors.append(f"{cfg['faq_questions_count']} questions attendues dans la FAQ "
                      f"(trouvé : {len(faq) if isinstance(faq, list) else 0})")
    else:
        for i, item in enumerate(faq, 1):
            if not isinstance(item, dict) or not item.get("question") or not item.get("answer"):
                errors.append(f"question de FAQ n°{i} incomplète")

    # Maillage interne : toujours dépendant du modèle, donc toujours contrôlé.
    body = " ".join(
        [data.get("lede", "")] +
        [b.get("text", "") + " " + " ".join(b.get("items") or [])
         for s in (sections if isinstance(sections, list) else [])
         if isinstance(s, dict)
         for b in (s.get("content") or []) if isinstance(b, dict)])
    links = re.findall(r"\[[^\]]+\]\((/[^)\s]*)\)", body)
    targets = cfg["internal_link_targets"]
    if sum(1 for h in links if h in targets) < 2:
        errors.append("maillage interne : moins de deux liens vers "
                      + " ou ".join(targets))
    if not any(h.startswith("/blog") for h in links):
        errors.append("maillage interne : aucun lien vers /blog/")

    wc = content_word_count(data)
    if not MIN_WORDS <= wc <= MAX_WORDS:
        errors.append(f"volume hors bornes : {wc} mots (attendu {MIN_WORDS}–{MAX_WORDS})")

    return errors


# ─────────────────────────────────────────────────────────────
# Assemblage du HTML à partir du gabarit
# ─────────────────────────────────────────────────────────────

MAIN_OPEN = '<main id="contenu">'


def extract_balanced_div(html: str, class_name: str) -> str:
    """Extrait un <div class="…"> avec ses div imbriqués.

    Le bloc CTA du gabarit contient un div interne (.cta-actions) : une regex
    non gourmande s'arrêterait au premier </div>. On compte donc les balises.
    """
    start = html.find(f'<div class="{class_name}"')
    if start == -1:
        return ""
    depth, pos = 0, start
    for m in re.finditer(r"<div\b[^>]*>|</div>", html[start:]):
        depth += 1 if m.group().startswith("<div") else -1
        pos = start + m.end()
        if depth == 0:
            return html[start:pos]
    return ""


def split_template(reference_html: str) -> dict:
    """Découpe le gabarit relu en morceaux réutilisables. Tout ce qui n'est pas
    propre à un article (favicons, polices, header, fil d'Ariane, footer, script,
    bloc CTA) est repris tel quel : si le gabarit évolue, les articles suivants
    suivent.

    Conventions HTML propres à ce site, différentes de celles du gabarit d'origine :
      · pas de commentaire <!-- Article --> avant les JSON-LD : on découpe sur la
        première balise <script type="application/ld+json"> ;
      · le <main> porte un identifiant : <main id="contenu"> ;
      · le fil d'Ariane est un <nav class="breadcrumb"> situé AVANT le <main>,
        donc dans le morceau « header », où il est réécrit par article ;
      · le bloc CTA est un <div class="cta-box"> contenant un div imbriqué.
    """
    parts = {}

    ld_start = reference_html.find('<script type="application/ld+json">')
    head_end = reference_html.find("</head>")
    if ld_start == -1 or head_end == -1:
        raise ValueError("Gabarit : bloc JSON-LD ou </head> introuvable.")
    # Le <head> conservé s'arrête juste avant le premier JSON-LD : les trois
    # blocs sont entièrement refabriqués par build_jsonld().
    parts["head_top"] = reference_html[:ld_start]

    body_start = reference_html.find("<body>")
    main_start = reference_html.find(MAIN_OPEN)
    main_end = reference_html.find("</main>")
    if min(body_start, main_start, main_end) == -1:
        raise ValueError(f"Gabarit : <body>, {MAIN_OPEN} ou </main> introuvable.")

    # Entre </head> et <main> : ouverture du body, lien d'évitement, header de
    # site et fil d'Ariane.
    parts["header"] = reference_html[head_end + len("</head>"):main_start]
    parts["footer"] = reference_html[main_end:]            # </main> jusqu'à </html>

    if 'class="breadcrumb' not in parts["header"]:
        raise ValueError("Gabarit : fil d'Ariane introuvable avant le <main>.")

    parts["cta"] = extract_balanced_div(
        reference_html[main_start:main_end], "cta-box")
    return parts


def build_head(parts: dict, cfg: dict, data: dict, url: str, today: dict) -> str:
    """Reprend le <head> du gabarit et n'y remplace que ce qui est propre à
    l'article. Les valeurs viennent du script, jamais du modèle en HTML."""
    head = parts["head_top"]
    title = f"{plain(data['title'])} | {cfg['site_name']}"
    desc = plain(data["meta_description"])
    img = f"{cfg['site_url']}{cfg['og_image']}"
    section = cfg["default_article_section"]

    def swap(pattern: str, replacement: str, text: str) -> str:
        new, n = re.subn(pattern, lambda _: replacement, text, count=1)
        if n != 1:
            raise ValueError(f"Gabarit : motif introuvable dans le <head> — {pattern}")
        return new

    head = swap(r"<title>.*?</title>", f"<title>{esc(title)}</title>", head)
    head = swap(r'<meta name="description" content="[^"]*" />',
                f'<meta name="description" content="{esc(desc)}" />', head)
    head = swap(r'<link rel="canonical" href="[^"]*" />',
                f'<link rel="canonical" href="{url}" />', head)
    head = swap(r'<meta property="og:title" content="[^"]*" />',
                f'<meta property="og:title" content="{esc(plain(data["h1"]))}" />', head)
    head = swap(r'<meta property="og:description" content="[^"]*" />',
                f'<meta property="og:description" content="{esc(desc)}" />', head)
    head = swap(r'<meta property="og:url" content="[^"]*" />',
                f'<meta property="og:url" content="{url}" />', head)
    head = swap(r'<meta property="og:image" content="[^"]*" />',
                f'<meta property="og:image" content="{img}" />', head)
    head = swap(r'<meta property="article:published_time" content="[^"]*" />',
                f'<meta property="article:published_time" content="{today["iso"]}" />', head)
    head = swap(r'<meta property="article:modified_time" content="[^"]*" />',
                f'<meta property="article:modified_time" content="{today["iso"]}" />', head)
    head = swap(r'<meta property="article:section" content="[^"]*" />',
                f'<meta property="article:section" content="{esc(section)}" />', head)
    head = swap(r'<meta name="twitter:title" content="[^"]*" />',
                f'<meta name="twitter:title" content="{esc(plain(data["title"]))}" />', head)
    head = swap(r'<meta name="twitter:description" content="[^"]*" />',
                f'<meta name="twitter:description" content="{esc(desc)}" />', head)
    head = swap(r'<meta name="twitter:image" content="[^"]*" />',
                f'<meta name="twitter:image" content="{img}" />', head)
    return head


def build_jsonld(cfg: dict, data: dict, url: str, today: dict) -> str:
    """Les trois blocs JSON-LD, sérialisés par json.dumps : ils sont valides
    par construction, ce que le modèle ne pouvait pas garantir."""
    img = f"{cfg['site_url']}{cfg['og_image']}"
    nap = cfg.get("nap", {})
    publisher = {
        "@type": "Organization", "name": cfg["site_name"],
        "url": f"{cfg['site_url']}/",
        "logo": {"@type": "ImageObject",
                 "url": f"{cfg['site_url']}{cfg['logo_path']}"},
    }
    if nap:
        publisher["telephone"] = nap.get("phone_e164", "")
        publisher["email"] = nap.get("email", "")
        publisher["address"] = {
            "@type": "PostalAddress",
            "streetAddress": nap.get("street", ""),
            "postalCode": nap.get("postal_code", ""),
            "addressLocality": nap.get("city", ""),
            "addressRegion": nap.get("region", ""),
            "addressCountry": nap.get("country", "FR"),
        }

    article = {
        "@context": "https://schema.org",
        "@type": "Article",
        "@id": f"{url}#article",
        "headline": plain(data["h1"]),
        "description": plain(data["meta_description"]),
        "inLanguage": "fr-FR",
        "datePublished": today["iso"],
        "dateModified": today["iso"],
        "image": img,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "author": {"@type": "Organization", "name": cfg["author"],
                   "url": f"{cfg['site_url']}/"},
        "publisher": publisher,
        # La page d'accueil déclare un EventVenue sans @id : on décrit donc le
        # sujet par des entités nommées plutôt que par une référence qui
        # pointerait dans le vide.
        "about": [
            {"@type": "Thing", "name": cfg["default_article_section"]},
            {"@type": "Place", "name": "Hautes-Pyrénées"},
            {"@type": "Place", "name": nap.get("city", cfg["location"].split(",")[0])},
        ],
        "isPartOf": {"@id": f"{cfg['site_url']}/blog/#blog"},
        "articleSection": cfg["default_article_section"],
        "keywords": ", ".join(cfg["geo_keywords"][:6]),
    }
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Accueil",
             "item": f"{cfg['site_url']}/"},
            {"@type": "ListItem", "position": 2, "name": "Journal",
             "item": f"{cfg['site_url']}/blog/"},
            {"@type": "ListItem", "position": 3, "name": plain(data["title"]),
             "item": url},
        ],
    }
    faqpage = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": plain(q["question"]),
             "acceptedAnswer": {"@type": "Answer", "text": plain(q["answer"])}}
            for q in data["faq"]
        ],
    }
    out = []
    for payload in (article, breadcrumb, faqpage):
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        body = "\n".join("  " + l if l.strip() else l for l in body.splitlines())
        out.append(f'  <script type="application/ld+json">\n{body}\n  </script>\n')
    return "\n".join(out)


def render_blocks(blocks: list[dict]) -> str:
    """Contenu d'une section, converti en HTML. Le modèle n'écrit que du texte :
    c'est ici, et seulement ici, que le balisage apparaît."""
    out = []
    for block in blocks:
        kind = block.get("type")
        if kind in ("ul", "ol"):
            items = block.get("items")
            if not items:
                items = [s for s in re.split(r"\s*[;\n]\s*", block.get("text", "")) if s]
            lines = "\n".join(f"          <li>{inline(i)}</li>" for i in items)
            out.append(f"        <{kind}>\n{lines}\n        </{kind}>")
        elif kind == "h3":
            out.append(f"        <h3>{inline(block['text'])}</h3>")
        elif kind == "strong":
            out.append(f"        <p><strong>{inline(block['text'])}</strong></p>")
        else:
            out.append(f"        <p>{inline(block['text'])}</p>")
    return "\n".join(out)


def build_breadcrumb(header: str, data: dict) -> str:
    """Réécrit le dernier maillon du fil d'Ariane, qui vit hors du <main> sur
    ce site. Le reste du header est repris tel quel."""
    label = plain(data["breadcrumb"]).strip()
    if len(label) > 60:
        label = label[:57].rsplit(" ", 1)[0] + "…"
    new, n = re.subn(
        r'(<nav class="breadcrumb[^"]*"[^>]*>.*?)<li>[^<]*</li>(\s*</ol>)',
        lambda m: m.group(1) + f"<li>{inline(label)}</li>" + m.group(2),
        header, count=1, flags=re.S)
    if n != 1:
        raise ValueError("Gabarit : dernier maillon du fil d'Ariane introuvable.")
    return new


def build_main(parts: dict, cfg: dict, data: dict, today: dict) -> str:
    """Le <main> complet, aux conventions de ce site : article > article-head
    (post-meta, h1, chapô) puis article-body (corps, FAQ en <details>, CTA)."""
    reading = max(3, round(content_word_count(data) / 200))
    body = "\n\n".join(
        f"        <h2>{inline(s['h2'])}</h2>\n{render_blocks(s['content'])}"
        for s in data["sections"])

    faq = "\n".join(
        "          <details>\n"
        f'            <summary>{inline(q["question"])}</summary>\n'
        f'            <div><p>{inline(q["answer"])}</p></div>\n'
        "          </details>"
        for q in data["faq"])

    # Le bloc CTA est recopié tel quel du gabarit : seule la première ligne perd
    # son indentation à l'extraction, on la lui rend. Les lignes suivantes
    # gardent la leur, donc l'imbrication du gabarit est préservée.
    cta = ("\n\n        " + parts["cta"]) if parts["cta"] else ""

    return f"""{MAIN_OPEN}
    <article class="article">
      <div class="wrap article-head">
        <div class="post-meta">
          <span class="tag">{esc(cfg['default_article_section'])}</span>
          <time datetime="{today['iso']}">{today['fr']}</time>
          <span>Lecture {reading} min</span>
        </div>
        <h1>{inline(data['h1'])}</h1>
        <p class="lede">{inline(data['lede'])}</p>
      </div>

      <div class="wrap article-body">

{body}

        <h2>Questions fréquentes</h2>
        <div class="faq">
{faq}
        </div>{cta}

      </div>
    </article>
  """


def assemble(reference_html: str, cfg: dict, topic: dict,
             data: dict, today: dict) -> str:
    """Fabrique la page complète. Toute la structure vient d'ici : le modèle
    n'a produit que du texte."""
    parts = split_template(reference_html)
    url = f"{cfg['site_url']}/blog/{topic['slug']}/"
    marker = f"<!-- {cfg['topic_marker_prefix']}: {topic['num']} -->"

    head = build_head(parts, cfg, data, url, today)
    jsonld = build_jsonld(cfg, data, url, today)
    header = build_breadcrumb(parts["header"], data)
    header = header.replace("<body>", f"<body>\n{marker}", 1)

    return (head + jsonld + "</head>" + header
            + build_main(parts, cfg, data, today) + parts["footer"])


def validate_assembled(html: str, cfg: dict, topic: dict) -> list[str]:
    """Filet de sécurité sur l'assemblage : ces contrôles ne portent plus sur le
    modèle mais sur notre propre code. Ils doivent toujours passer."""
    errors = []
    url = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if not html.startswith("<!DOCTYPE html>"):
        errors.append("assemblage : DOCTYPE absent")
    if not html.rstrip().endswith("</html>"):
        errors.append("assemblage : </html> absent")
    if f"{cfg['topic_marker_prefix']}: {topic['num']}" not in html:
        errors.append("assemblage : marqueur d'idempotence absent")
    if html.count("<h1") != 1:
        errors.append(f"assemblage : {html.count('<h1')} balise(s) h1")
    if f'rel="canonical" href="{url}"' not in html:
        errors.append("assemblage : canonical incorrect")
    if "/assets/blog.css" not in html:
        errors.append("assemblage : feuille de style du blog absente")
    if html.count(MAIN_OPEN) != 1 or html.count("</main>") != 1:
        errors.append("assemblage : balise <main> déséquilibrée")
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if len(blocks) != 3:
        errors.append(f"assemblage : {len(blocks)} blocs JSON-LD au lieu de 3")
    for i, block in enumerate(blocks, 1):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            errors.append(f"assemblage : JSON-LD n°{i} invalide ({exc})")
    if html.count("<summary>") != cfg["faq_questions_count"]:
        errors.append(f"assemblage : {html.count('<summary>')} questions de FAQ "
                      f"au lieu de {cfg['faq_questions_count']}")
    # Parité FAQ entre le HTML affiché et le JSON-LD, exigée par les moteurs.
    faq_ld = [b for b in blocks if '"FAQPage"' in b]
    if faq_ld:
        names = [q["name"] for q in json.loads(faq_ld[0])["mainEntity"]]
        shown = [re.sub(r"<[^>]+>", "", s) for s in
                 re.findall(r"<summary>(.*?)</summary>", html, re.S)]
        unescape = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"'}
        for k, v in unescape.items():
            shown = [s.replace(k, v) for s in shown]
        if names != shown:
            errors.append("assemblage : FAQ affichée et FAQPage divergent")
    return errors


def extract(data: dict) -> dict:
    """Métadonnées utilisées par blog/index.html, rss.xml et llms.txt."""
    return {
        "title": plain(data["title"]),
        "description": plain(data["meta_description"]),
        "h1": plain(data["h1"]),
        "headline": plain(data["h1"]),
        "lead": plain(data["lede"]),
        "words": content_word_count(data),
    }


# ─────────────────────────────────────────────────────────────
# Mises à jour des fichiers annexes
# ─────────────────────────────────────────────────────────────

def teaser_of(meta: dict) -> str:
    teaser = meta["description"] or meta["lead"]
    if len(teaser) > 320:
        teaser = teaser[:317].rsplit(" ", 1)[0] + "…"
    return teaser


def update_blog_index(cfg: dict, topic: dict, meta: dict, today: dict) -> str:
    """Insère la carte de l'article en tête de la liste.

    Convention locale : la liste est un <div class="post-list"> et chaque carte
    est un <a class="post-card"> — et non un <article>.
    """
    html = BLOG_INDEX.read_text(encoding="utf-8")
    url = f"/blog/{topic['slug']}/"
    if url in html:
        log("blog/index.html contient déjà cet article : pas de doublon ajouté.")
        return html

    headline = meta["headline"] or meta["h1"] or topic["title"]
    reading = max(3, round(meta["words"] / 200))

    card = f"""
        <a class="post-card" href="{url}">
          <div class="post-meta">
            <span class="tag">{esc(cfg['default_article_section'])}</span>
            <time datetime="{today['iso']}">{today['fr']}</time>
            <span>Lecture {reading} min</span>
          </div>
          <h2>{esc(headline)}</h2>
          <p>{esc(teaser_of(meta))}</p>
          <span class="post-more">
            Lire l'article
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m9 5 7 7-7 7"/></svg>
          </span>
        </a>
"""
    anchor = '<div class="post-list">'
    if anchor not in html:
        raise ValueError("Point d'insertion .post-list introuvable dans blog/index.html")
    html = html.replace(anchor, anchor + card, 1)

    entry = f"""{{
      "@type": "BlogPosting",
      "headline": {json.dumps(headline, ensure_ascii=False)},
      "url": "{cfg['site_url']}{url}",
      "datePublished": "{today['iso']}"
    }}, """
    ld_anchor = '"blogPost": ['
    if ld_anchor in html:
        html = html.replace(ld_anchor, ld_anchor + entry, 1)
    else:
        log("Avertissement : tableau blogPost introuvable, JSON-LD de l'index inchangé.")
    return html


def update_sitemap(cfg: dict, topic: dict, today: dict) -> str:
    xml = SITEMAP.read_text(encoding="utf-8")
    loc = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if loc in xml:
        log("sitemap.xml contient déjà cette URL.")
        return xml

    xml = re.sub(
        rf"(<loc>{re.escape(cfg['site_url'])}/blog/</loc>\s*<lastmod>)[^<]*(</lastmod>)",
        rf"\g<1>{today['iso']}\g<2>", xml)

    entry = f"""  <url>
    <loc>{loc}</loc>
    <lastmod>{today['iso']}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
</urlset>"""
    return xml.replace("</urlset>", entry, 1)


def update_rss(cfg: dict, topic: dict, meta: dict, today: dict) -> str:
    xml = RSS.read_text(encoding="utf-8")
    link = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if link in xml:
        log("rss.xml contient déjà cet article.")
        return xml

    def xesc(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    headline = meta["headline"] or meta["h1"] or topic["title"]
    pub = rfc822(today["date"])

    xml = re.sub(r"<lastBuildDate>[^<]*</lastBuildDate>",
                 f"<lastBuildDate>{pub}</lastBuildDate>", xml, count=1)

    item = f"""    <item>
      <title>{xesc(headline)}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <pubDate>{pub}</pubDate>
      <category>{xesc(cfg['default_article_section'])}</category>
      <description>{xesc(teaser_of(meta))}</description>
    </item>

"""
    if "    <item>" in xml:
        idx = xml.index("    <item>")
        return xml[:idx] + item + xml[idx:]
    return xml.replace("  </channel>", item + "  </channel>", 1)


def update_llms(cfg: dict, topic: dict, meta: dict) -> str | None:
    """Ajoute l'article à la liste des pages de llms.txt.

    Convention locale : la section s'intitule « ## Pages principales » et se
    termine par la ligne des mentions légales, devant laquelle on insère.
    """
    if not LLMS.exists():
        return None
    text = LLMS.read_text(encoding="utf-8")
    url = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if url in text:
        log("llms.txt référence déjà cet article.")
        return text

    headline = meta["headline"] or meta["h1"] or topic["title"]
    summary = (meta["description"] or "").rstrip(".")
    line = f"- [{headline}]({url}) : {summary}.\n"

    m = re.search(r"^## Pages principales\s*$(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    if not m:
        log("Avertissement : section « ## Pages principales » introuvable dans llms.txt.")
        return text

    block = m.group(1)
    legal = re.search(r"^- \[Mentions légales\].*$", block, flags=re.M)
    if legal:
        new_block = block[:legal.start()] + line + block[legal.start():]
    else:
        new_block = block.rstrip("\n") + "\n" + line + "\n"
    return text[:m.start(1)] + new_block + text[m.end(1):]


# ─────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────

def refresh_entries(cfg: dict, topic: dict, meta: dict) -> list[str]:
    """Après réécriture d'un article existant, resynchronise le teaser de
    blog/index.html et l'entrée RSS : les updaters sont idempotents par URL et
    laisseraient sinon en place le texte de l'ancienne version."""
    touched = []
    slug = topic["slug"]
    teaser = teaser_of(meta)

    html = BLOG_INDEX.read_text(encoding="utf-8")
    card = re.search(r'<a class="post-card" href="/blog/' + re.escape(slug)
                     + r'/">(?:(?!</a>).)*?</a>', html, re.S)
    if card:
        new_card = re.sub(r"<h2>[^<]*</h2>", f"<h2>{esc(meta['headline'])}</h2>",
                          card.group(), count=1)
        new_card = re.sub(r"<p>(?!<)[^<]*</p>", f"<p>{esc(teaser)}</p>",
                          new_card, count=1)
        if new_card != card.group():
            BLOG_INDEX.write_text(html.replace(card.group(), new_card, 1), encoding="utf-8")
            touched.append("blog/index.html")

    xml = RSS.read_text(encoding="utf-8")
    item = re.search(r"<item>(?:(?!</item>).)*?" + re.escape(slug)
                     + r"(?:(?!</item>).)*?</item>", xml, re.S)
    if item:
        new_item = re.sub(r"<description>.*?</description>",
                          f"<description>{esc(teaser)}</description>",
                          item.group(), count=1, flags=re.S)
        new_item = re.sub(r"<title>.*?</title>",
                          f"<title>{esc(meta['headline'])}</title>",
                          new_item, count=1, flags=re.S)
        if new_item != item.group():
            RSS.write_text(xml.replace(item.group(), new_item, 1), encoding="utf-8")
            touched.append("rss.xml")
    return touched


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Génère un article de blog du Belvédère du Domaine.")
    parser.add_argument("--dry-run", action="store_true",
                        help="n'écrit aucun fichier, affiche le résultat")
    parser.add_argument("--mock", action="store_true",
                        help="n'appelle pas l'API OpenAI (contenu de démonstration)")
    parser.add_argument("--rewrite", metavar="SLUG",
                        help="réécrit un article existant et écrase son fichier")
    parser.add_argument("--topics-only", action="store_true",
                        help="regarnit seulement la liste de sujets, sans rédiger")
    args = parser.parse_args()

    if args.topics_only and args.rewrite:
        fail("--topics-only et --rewrite sont incompatibles : le premier ne "
             "rédige aucun article, le second en réécrit un.")
        return EXIT_ERROR

    if args.dry_run:
        log("Mode DRY-RUN : aucun fichier ne sera écrit.")

    try:
        cfg = load_config()
        log(f"Site : {cfg['site_name']} — {cfg['site_url']}")

        if not WORKFLOW_PATH.exists():
            fail(f"BLOG_WORKFLOW.md introuvable ({WORKFLOW_PATH}).")
            return EXIT_ERROR
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        topics = parse_topics(workflow)
        rules = parse_editorial_rules(workflow)
        log(f"{len(topics)} sujets listés dans BLOG_WORKFLOW.md.")
        if not rules:
            log("Avertissement : règles éditoriales non trouvées, prompt allégé.")

        done, slugs = scan_blog(cfg["topic_marker_prefix"])
        log(f"Articles déjà en ligne : {len(slugs)} — sujets marqués traités : "
            f"{sorted(done) if done else 'aucun'}")

        # Réapprovisionnement AVANT la rédaction : si la réserve est vide, le
        # run du jour publie quand même. En mode réécriture, aucun sujet n'est
        # consommé, donc rien à regarnir.
        if not args.rewrite:
            try:
                if replenish_topics(cfg, topics, done, slugs,
                                    mock=args.mock, dry_run=args.dry_run):
                    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
                    topics = parse_topics(workflow)
                    log(f"{len(topics)} sujets après réapprovisionnement.")
            except Exception as exc:                      # noqa: BLE001
                # En mode --topics-only c'est le cœur du travail : l'erreur
                # remonte. Sinon elle ne doit JAMAIS empêcher la publication.
                if args.topics_only:
                    raise
                fail(f"Réapprovisionnement impossible ({type(exc).__name__} : "
                     f"{exc}) — la rédaction continue avec la réserve existante.")

        if args.topics_only:
            log("Mode --topics-only : terminé, aucun article rédigé.")
            return EXIT_OK

        if args.rewrite:
            # Réécriture : on retrouve le sujet par le marqueur du fichier existant.
            target_file = BLOG_DIR / args.rewrite / "index.html"
            if not target_file.exists():
                fail(f"Article introuvable : {target_file.relative_to(ROOT)}")
                return EXIT_ERROR
            existing = target_file.read_text(encoding="utf-8")
            m = re.search(rf"<!--\s*{re.escape(cfg['topic_marker_prefix'])}:\s*(\d+)\s*-->",
                          existing)
            if not m:
                fail(f"Aucun marqueur de sujet dans {target_file.relative_to(ROOT)} : "
                     "impossible de savoir quel sujet réécrire.")
                return EXIT_ERROR
            num = int(m.group(1))
            topic = next((t for t in topics if t["num"] == num), None)
            if topic is None:
                fail(f"Le sujet n°{num} n'existe plus dans BLOG_WORKFLOW.md.")
                return EXIT_ERROR
            topic["slug"] = args.rewrite
            log(f"Mode RÉÉCRITURE : sujet n°{num} — {topic['title']}")
        else:
            topic = pick_topic(topics, done, slugs)
            if topic is None:
                log("Aucun sujet restant à traiter. Ajoutez des sujets dans "
                    "BLOG_WORKFLOW.md (tableau des sujets suggérés).")
                return EXIT_NOTHING_TODO
            log(f"Sujet retenu : n°{topic['num']} — {topic['title']}")
            target_file = BLOG_DIR / topic["slug"] / "index.html"
            if target_file.exists():
                fail(f"Le fichier existe déjà : {target_file.relative_to(ROOT)} — "
                     "rien n'est écrasé (--rewrite pour le régénérer).")
                return EXIT_NOTHING_TODO

        log(f"Slug : {topic['slug']}")

        ref_slug, reference_html = load_reference_article(cfg, slugs)
        log(f"Gabarit relu depuis /blog/{ref_slug}/index.html "
            f"({len(reference_html)} caractères).")

        today_date = dt.date.today()
        today = {"date": today_date, "iso": today_date.isoformat(),
                 "fr": fr_date(today_date)}

        system = user = None
        if args.mock:
            log("Mode MOCK : contenu de démonstration, aucun appel API.")
            data = mock_content(cfg, topic)
        else:
            system, user = build_prompt(cfg, topic, rules)
            log(f"Prompt construit ({len(system)} car. système + "
                f"{len(user)} car. utilisateur).")
            data = generate_content(cfg, system, user)

        errors = validate_content(data, cfg)
        wc = content_word_count(data)

        # Rattrapage : on relance tant qu'il reste une erreur que le modèle peut
        # corriger — volume hors cible, maillage absent, etc. —, dans la limite
        # de MAX_CALLS appels au total. Chaque reprise repart de la MEILLEURE
        # copie obtenue jusque-là, pas de la dernière.
        calls = 1
        while (not args.mock and calls < MAX_CALLS
               and (errors or not PROMPT_MIN_WORDS <= wc <= MAX_WORDS)):
            correction = build_correction(cfg, errors, wc)
            calls += 1
            reason = (f"{wc} mots, cible {PROMPT_MIN_WORDS}"
                      if not PROMPT_MIN_WORDS <= wc <= MAX_WORDS
                      else f"{len(errors)} erreur(s) de validation")
            log(f"Copie à reprendre ({reason}) — tentative {calls}/{MAX_CALLS}.")
            try:
                retry = generate_content(cfg, system, user, followup=[
                    {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                    {"role": "user", "content": correction},
                ])
            except (ValueError, json.JSONDecodeError) as exc:
                fail(f"Tentative {calls} inexploitable : {exc}")
                break
            retry_errors = validate_content(retry, cfg)
            retry_wc = content_word_count(retry)
            log(f"Tentative {calls} : {retry_wc} mots, {len(retry_errors)} erreur(s).")
            if volume_rank(retry_errors, retry_wc) < volume_rank(errors, wc):
                data, errors, wc = retry, retry_errors, retry_wc
                log(f"Copie retenue : la n°{calls}.")
            else:
                log("Copie retenue : la précédente (la nouvelle n'est pas meilleure).")
        if calls > 1:
            log(f"{calls} appels OpenAI au total pour cet article.")

        if errors:
            fail("Contenu rejeté par la validation — aucun fichier écrit :")
            for err in errors:
                fail(f"  · {err}")
            return EXIT_ERROR

        html = assemble(reference_html, cfg, topic, data, today)
        build_errors = validate_assembled(html, cfg, topic)
        if build_errors:
            fail("Assemblage HTML incorrect — aucun fichier écrit :")
            for err in build_errors:
                fail(f"  · {err}")
            return EXIT_ERROR

        meta = extract(data)
        log("Validation OK.")
        log(f"  Titre       : {meta['title']}")
        log(f"  Description : {meta['description']} ({len(meta['description'])} car.)")
        log(f"  Volume      : {meta['words']} mots (corps hors FAQ)")
        log(f"  Page        : {len(html)} caractères, "
            f"{len(data['sections'])} sections")

        if args.dry_run:
            print("\n" + "═" * 70)
            print("APERÇU (aucun fichier écrit)")
            print("═" * 70)
            print(f"Sujet       : n°{topic['num']} — {topic['title']}")
            print(f"Slug        : {topic['slug']}")
            print(f"URL         : {cfg['site_url']}/blog/{topic['slug']}/")
            print(f"Titre       : {meta['title']}")
            print(f"H1          : {meta['h1']}")
            print(f"Description : {meta['description']}")
            print(f"Mots        : {meta['words']}")
            print("-" * 70)
            for section in data["sections"]:
                print(f"  H2 · {plain(section['h2'])}")
            print("═" * 70)
            log("DRY-RUN terminé, rien n'a été modifié.")
            return EXIT_OK

        # ── Écriture (au plus tard possible, une fois tout validé) ──
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(html, encoding="utf-8")
        log(f"Écrit : {target_file.relative_to(ROOT)}")

        if args.rewrite:
            for name in refresh_entries(cfg, topic, meta):
                log(f"Resynchronisé : {name}")
            log(f"Terminé — article n°{topic['num']} réécrit : "
                f"{cfg['site_url']}/blog/{topic['slug']}/")
            return EXIT_OK

        blog_index_html = update_blog_index(cfg, topic, meta, today)
        sitemap_xml = update_sitemap(cfg, topic, today)
        rss_xml = update_rss(cfg, topic, meta, today)
        llms_txt = update_llms(cfg, topic, meta)

        BLOG_INDEX.write_text(blog_index_html, encoding="utf-8")
        log("Mis à jour : blog/index.html")
        SITEMAP.write_text(sitemap_xml, encoding="utf-8")
        log("Mis à jour : sitemap.xml")
        RSS.write_text(rss_xml, encoding="utf-8")
        log("Mis à jour : rss.xml")
        if llms_txt is not None:
            LLMS.write_text(llms_txt, encoding="utf-8")
            log("Mis à jour : llms.txt")

        log(f"Terminé — article n°{topic['num']} publié : "
            f"{cfg['site_url']}/blog/{topic['slug']}/")
        return EXIT_OK

    except Exception as exc:                      # noqa: BLE001
        fail(f"{type(exc).__name__} : {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
