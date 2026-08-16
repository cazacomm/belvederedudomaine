#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génération automatique d'un article de blog — Le Belvédère du Domaine.

Principe : le gabarit HTML n'est JAMAIS dupliqué dans ce script. Il est relu à
chaque exécution depuis l'article de référence déjà publié dans /blog/ (le plus
ancien, d'après son `datePublished`). Seuls les champs variables sont remplacés.
Toute la mise en page, le CSS, l'en-tête et le pied de page restent donc
strictement identiques à l'existant.

Sorties :
  exit 0   article généré (ou dry-run réussi)
  exit 1   erreur (API, parsing, écriture…)
  exit 78  aucun nouveau sujet à traiter — rien à faire

Usage :
  python3 scripts/generate-article.py [--dry-run] [--topic N] [--config PATH]
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover - environnement sans tzdata
    TZ = None

ROOT = Path(__file__).resolve().parent.parent

MOIS_FR = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril", 5: "mai", 6: "juin",
    7: "juillet", 8: "août", 9: "septembre", 10: "octobre", 11: "novembre",
    12: "décembre",
}


# ─────────────────────────────────────────────────────────────────────────────
# Journalisation
# ─────────────────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[blog] {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"[blog][ERREUR] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    if not path.exists():
        die(f"configuration introuvable : {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"blog-config.json illisible : {exc}")
    for key in ("site_name", "site_url", "sector", "location"):
        if not cfg.get(key):
            die(f"clé manquante dans blog-config.json : {key}")
    cfg["site_url"] = cfg["site_url"].rstrip("/")
    cfg.setdefault("paths", {})
    return cfg


def site_slug(cfg: dict) -> str:
    """Identifiant court du site, déduit du domaine — sert au marqueur d'idempotence."""
    host = re.sub(r"^https?://", "", cfg["site_url"]).split("/")[0]
    return host.split(".")[0].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Sujets : extraction depuis BLOG_WORKFLOW.md
# ─────────────────────────────────────────────────────────────────────────────
def parse_topics(doc_path: Path) -> list[dict]:
    """Lit le tableau « Douze sujets à traiter » de BLOG_WORKFLOW.md."""
    if not doc_path.exists():
        die(f"BLOG_WORKFLOW.md introuvable : {doc_path}")
    text = doc_path.read_text(encoding="utf-8")

    topics: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if not re.fullmatch(r"\d+", cells[0]):
            continue
        # Un sujet publié manuellement est signalé par « publié » dans la colonne Angle
        # (cf. BLOG_WORKFLOW.md §7). Les articles générés, eux, portent un marqueur HTML.
        published = bool(re.search(r"publi[ée]s?\b", cells[2], re.I))
        angle = re.sub(r"\*\*(.*?)\*\*", r"\1", cells[2]).strip()
        angle = re.sub(r"\s*[—-]\s*publi[ée]s?\s*$", "", angle, flags=re.I).strip()

        topics.append({
            "n": int(cells[0]),
            "subject": re.sub(r"\*\*(.*?)\*\*", r"\1", cells[1]).strip(),
            "angle": angle,
            "audience": cells[3].strip(),
            "published": published,
        })

    if not topics:
        die("aucun sujet trouvé dans BLOG_WORKFLOW.md (tableau §7 absent ou modifié)")
    topics.sort(key=lambda t: t["n"])
    flagged = [t["n"] for t in topics if t["published"]]
    log(f"{len(topics)} sujets lus depuis {doc_path.name} "
        f"(marqués publiés dans le tableau : {flagged or 'aucun'})")
    return topics


# ─────────────────────────────────────────────────────────────────────────────
# Articles existants
# ─────────────────────────────────────────────────────────────────────────────
def scan_existing(blog_dir: Path, marker_prefix: str) -> tuple[set[int], set[str], list[tuple[str, Path]]]:
    """Retourne (numéros de sujets déjà traités, slugs existants, [(datePublished, path)])."""
    done_topics: set[int] = set()
    slugs: set[str] = set()
    dated: list[tuple[str, Path]] = []

    for path in sorted(blog_dir.glob("*/index.html")):
        slug = path.parent.name
        slugs.add(slug)
        raw = path.read_text(encoding="utf-8")

        m = re.search(rf"<!--\s*{re.escape(marker_prefix)}-topic:\s*(\d+)\s*-->", raw)
        if m:
            done_topics.add(int(m.group(1)))

        d = re.search(r'"datePublished"\s*:\s*"([0-9-]+)"', raw)
        dated.append((d.group(1) if d else "9999-99-99", path))

    log(f"articles existants : {len(slugs)} ({', '.join(sorted(slugs)) or 'aucun'})")
    log(f"sujets déjà traités (marqueurs) : {sorted(done_topics) or 'aucun'}")
    return done_topics, slugs, dated


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-")[:80]


def topic_matches_existing(topic: dict, slugs: set[str]) -> bool:
    """Filet de sécurité : détecte un sujet déjà couvert par un slug proche."""
    candidate = slugify(topic["subject"])
    cand_words = {w for w in candidate.split("-") if len(w) > 4}
    for slug in slugs:
        slug_words = {w for w in slug.split("-") if len(w) > 4}
        if not cand_words or not slug_words:
            continue
        overlap = len(cand_words & slug_words) / len(cand_words)
        if overlap >= 0.75:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Gabarit : relu depuis l'article de référence
# ─────────────────────────────────────────────────────────────────────────────
def pick_template(dated: list[tuple[str, Path]]) -> Path:
    if not dated:
        die("aucun article de référence dans /blog/ — impossible de déduire le gabarit")
    dated = sorted(dated, key=lambda x: (x[0], str(x[1])))
    ref = dated[0][1]
    log(f"gabarit relu depuis : {ref.relative_to(ROOT)}")
    return ref


def replace_ld(doc: str, type_name: str, payload: dict) -> str:
    """Remplace le bloc JSON-LD dont le @type correspond."""
    pattern = re.compile(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', re.S
    )
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    body = "\n".join("  " + l if l.strip() else l for l in body.splitlines())
    replacement = f'<script type="application/ld+json">\n{body}\n  </script>'

    out, found = [], False
    last = 0
    for m in pattern.finditer(doc):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if parsed.get("@type") == type_name and not found:
            out.append(doc[last:m.start()])
            out.append(replacement)
            last = m.end()
            found = True
    if not found:
        raise ValueError(f"bloc JSON-LD {type_name} absent du gabarit")
    out.append(doc[last:])
    return "".join(out)


def sub_once(doc: str, pattern: str, replacement: str, label: str, flags=0) -> str:
    new, n = re.subn(pattern, lambda _m: replacement, doc, count=1, flags=flags)
    if n != 1:
        raise ValueError(f"gabarit : motif introuvable — {label}")
    return new


# ─────────────────────────────────────────────────────────────────────────────
# Appel OpenAI
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA_HINT = """{
  "slug": "slug-en-minuscules-sans-accents-4-a-6-mots",
  "meta_title": "titre <= 60 caractères",
  "h1": "titre de l'article",
  "meta_description": "promesse concrète, STRICTEMENT moins de 150 caractères",
  "category": "Mariage | Séminaire | Réception privée | Local",
  "reading_time": 7,
  "card_excerpt": "résumé de 200 à 320 caractères pour la carte de la liste d'articles",
  "lede": "chapeau de 2 à 3 phrases",
  "sections": [
    {
      "h2": "titre de section",
      "paragraphs": ["paragraphe", "paragraphe"],
      "h3": "sous-titre optionnel ou null",
      "h3_paragraphs": ["paragraphe sous le h3"],
      "list": ["puce", "puce", "puce"],
      "blockquote": "citation courte ou null"
    }
  ],
  "faq": [{ "question": "...", "answer": "..." }]
}"""


def build_prompt(cfg: dict, topic: dict, existing_titles: list[str]) -> tuple[str, str]:
    nap = cfg.get("nap", {})
    facts = cfg.get("facts_allowed", [])
    geo = cfg.get("geo_keywords", [])

    system = (
        "Tu es rédacteur web SEO senior pour un établissement français. "
        "Tu écris un français impeccable, sobre et précis. "
        "Tu ne produis QUE du JSON valide, sans texte autour, sans bloc de code markdown."
    )

    user = f"""Rédige un article de blog pour {cfg['site_name']}.

CONTEXTE ÉTABLISSEMENT
- Secteur : {cfg['sector']}
- Lieu : {cfg['location']} — {nap.get('street','')}, {nap.get('postal_code','')} {nap.get('city','')}
- Téléphone : {nap.get('phone_display','')} — E-mail : {nap.get('email','')}
- Site : {cfg['site_url']}
- Ton attendu : {cfg['tone']}

SUJET IMPOSÉ (n°{topic['n']})
- Titre de travail : {topic['subject']}
- Angle : {topic['angle']}
- Cible : {topic['audience']}

CONTRAINTES DE FOND — IMPÉRATIVES
- Longueur totale visée : environ {cfg.get('target_word_count', 1300)} mots (entre 1200 et 1500).
- 6 à 8 sections `h2`. Au moins deux sections comportent un `h3`, au moins une comporte une liste
  de 4 à 5 puces, au maximum UNE section comporte une citation (`blockquote`).
- Exactement {cfg.get('faq_questions_count', 5)} questions de FAQ, en fin d'article, avec des
  réponses de 2 à 4 phrases, autonomes et directement utiles.
- Ancrage local obligatoire et intégré à l'argumentation (jamais plaqué en fin de phrase) :
  {', '.join(geo)}.
- N'INVENTE JAMAIS : aucun prix, tarif, forfait ou acompte ; aucun chiffre précis non fourni
  (superficies, distances en km, nombre de chambres, nombre d'événements par an, taux) ;
  aucun nom de client, témoignage ou citation attribuée ; aucune réglementation, norme ERP,
  obligation légale ou horaire d'arrêté ; aucune date de fondation ou d'historique du lieu.
- Les SEULS faits chiffrés ou matériels autorisés sur l'établissement sont :
  {'; '.join(facts)}.
- En cas de doute, formule qualitativement (« à courte distance de Tarbes ») et jamais
  quantitativement (« à 18 km de Tarbes »).
- Pas de superlatif creux, pas de point d'exclamation, pas de « n'hésitez pas ».
- Parle de l'établissement à la première personne du pluriel, avec parcimonie.

CONTRAINTES DE FORME
- Texte brut uniquement dans les champs : AUCUNE balise HTML, AUCUN markdown, aucun lien.
- Les apostrophes sont des apostrophes typographiques françaises (’) ou droites, au choix,
  mais jamais d'entités HTML.
- `meta_description` : strictement moins de 150 caractères.
- `slug` : minuscules, tirets, sans accent, 4 à 6 mots, mot-clé principal + ancrage local.

ARTICLES DÉJÀ EN LIGNE (ne pas répéter leur contenu, angles différents obligatoires) :
{chr(10).join('- ' + t for t in existing_titles) or '- aucun'}

FORMAT DE SORTIE — un unique objet JSON respectant exactement cette structure :
{SCHEMA_HINT}
"""
    return system, user


def call_openai(cfg: dict, system: str, user: str) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        die("variable d'environnement OPENAI_API_KEY absente ou vide")

    try:
        from openai import OpenAI
    except ImportError:
        die("paquet `openai` non installé — `pip install openai`")

    oc = cfg.get("openai", {})
    model = oc.get("model", "gpt-4o-mini")
    temperature = oc.get("temperature", 0.7)

    log(f"appel OpenAI — modèle={model} temperature={temperature}")
    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=oc.get("max_tokens", 6000),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as exc:
        die(f"appel OpenAI échoué : {type(exc).__name__} — {exc}")

    content = (resp.choices[0].message.content or "").strip()
    if not content:
        die("réponse OpenAI vide")

    usage = getattr(resp, "usage", None)
    if usage:
        log(f"jetons : prompt={usage.prompt_tokens} completion={usage.completion_tokens}")

    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        die(f"réponse OpenAI non JSON : {exc}\n---\n{content[:600]}\n---")


def validate_payload(data: dict, cfg: dict, topic: dict) -> dict:
    """Contrôle et normalise la sortie du modèle. Lève ValueError si irrécupérable."""
    required = ["slug", "meta_title", "h1", "meta_description", "lede", "sections", "faq"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ValueError(f"champs manquants dans la réponse : {', '.join(missing)}")

    data["slug"] = slugify(data["slug"]) or slugify(topic["subject"])

    desc = " ".join(str(data["meta_description"]).split())
    if len(desc) >= 155:
        cut = desc[:151].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        desc = cut + "…"
        log(f"meta description raccourcie à {len(desc)} caractères")
    data["meta_description"] = desc

    sections = [s for s in data["sections"] if isinstance(s, dict) and s.get("h2")]
    if len(sections) < 4:
        raise ValueError(f"seulement {len(sections)} sections h2 exploitables (minimum 4)")
    data["sections"] = sections

    faq = []
    for item in data["faq"]:
        if isinstance(item, dict) and item.get("question") and item.get("answer"):
            faq.append({
                "question": " ".join(str(item["question"]).split()),
                "answer": " ".join(str(item["answer"]).split()),
            })
    want = cfg.get("faq_questions_count", 5)
    if len(faq) < want:
        raise ValueError(f"{len(faq)} questions de FAQ reçues, {want} attendues")
    data["faq"] = faq[:want]

    try:
        data["reading_time"] = max(3, min(20, int(data.get("reading_time") or 7)))
    except (TypeError, ValueError):
        data["reading_time"] = 7

    data["category"] = str(data.get("category") or topic["audience"].split("/")[0]).strip() or "Journal"
    data["card_excerpt"] = " ".join(str(data.get("card_excerpt") or data["lede"]).split())
    data["meta_title"] = " ".join(str(data["meta_title"]).split())
    data["h1"] = " ".join(str(data["h1"]).split())
    data["lede"] = " ".join(str(data["lede"]).split())
    return data


def word_count(data: dict) -> int:
    parts = [data["lede"]]
    for s in data["sections"]:
        parts += s.get("paragraphs") or []
        parts += s.get("h3_paragraphs") or []
        parts += s.get("list") or []
        for k in ("h2", "h3", "blockquote"):
            if s.get(k):
                parts.append(s[k])
    for f in data["faq"]:
        parts += [f["question"], f["answer"]]
    return len(" ".join(str(p) for p in parts).split())


# ─────────────────────────────────────────────────────────────────────────────
# Rendu HTML
# ─────────────────────────────────────────────────────────────────────────────
def esc(text) -> str:
    """Échappement pour un nœud de texte — l'apostrophe reste littérale, comme sur le site."""
    return html_mod.escape(str(text), quote=False)


def esca(text) -> str:
    """Échappement pour une valeur d'attribut (guillemets compris)."""
    return html_mod.escape(str(text), quote=True)


def render_body(data: dict, cfg: dict, date_iso: str, date_fr: str) -> str:
    nap = cfg.get("nap", {})
    I = "        "  # indentation du gabarit dans .article-body
    out: list[str] = []

    out.append('    <article class="article">')
    out.append('      <div class="wrap article-head">')
    out.append('        <div class="post-meta">')
    out.append(f'          <span class="tag">{esc(data["category"])}</span>')
    out.append(f'          <time datetime="{date_iso}">{date_fr}</time>')
    out.append(f'          <span>Lecture {data["reading_time"]} min</span>')
    out.append("        </div>")
    out.append(f'        <h1>{esc(data["h1"])}</h1>')
    out.append(f'        <p class="lede">{esc(data["lede"])}</p>')
    out.append("      </div>")
    out.append("")
    out.append('      <div class="wrap article-body">')
    out.append("")

    for sec in data["sections"]:
        out.append(f'{I}<h2>{esc(sec["h2"])}</h2>')
        for p in sec.get("paragraphs") or []:
            out.append(f"{I}<p>{esc(p)}</p>")
        if sec.get("h3"):
            out.append(f'{I}<h3>{esc(sec["h3"])}</h3>')
            for p in sec.get("h3_paragraphs") or []:
                out.append(f"{I}<p>{esc(p)}</p>")
        items = sec.get("list") or []
        if items:
            out.append(f"{I}<ul>")
            for li in items:
                out.append(f"{I}  <li>{esc(li)}</li>")
            out.append(f"{I}</ul>")
        if sec.get("blockquote"):
            out.append(f'{I}<blockquote>{esc(sec["blockquote"])}</blockquote>')
        out.append("")

    out.append(f"{I}<h2>Questions fréquentes</h2>")
    out.append(f'{I}<div class="faq">')
    for f in data["faq"]:
        out.append(f"{I}  <details>")
        out.append(f'{I}    <summary>{esc(f["question"])}</summary>')
        out.append(f'{I}    <div><p>{esc(f["answer"])}</p></div>')
        out.append(f"{I}  </details>")
    out.append(f"{I}</div>")
    out.append("")

    out.append(f'{I}<div class="cta-box">')
    out.append(f'{I}  <h2>{esc(cfg["site_name"])} — {esc(nap.get("city", ""))} ({esc(nap.get("postal_code", "")[:2])})</h2>')
    out.append(
        f'{I}  <p>{esc(nap.get("street",""))}, {esc(nap.get("postal_code",""))} {esc(nap.get("city",""))}. '
        "Mariages, séminaires et réceptions privées avec vue panoramique sur les Pyrénées, "
        "espaces modulables et hébergement sur place.</p>"
    )
    out.append(f'{I}  <div class="cta-actions">')
    out.append(f'{I}    <a class="btn btn-solid" href="/#contact"><span>Demande pour votre événement</span></a>')
    out.append(f'{I}    <a class="btn" href="tel:{esca(nap.get("phone_e164",""))}"><span>{esc(nap.get("phone_display",""))}</span></a>')
    out.append(f"{I}  </div>")
    out.append(f"{I}</div>")
    out.append("")
    out.append("      </div>")
    out.append("    </article>")
    return "\n".join(out)


def render_article(template: str, data: dict, cfg: dict, topic: dict,
                   marker_prefix: str, date_iso: str, date_fr: str) -> str:
    base = cfg["site_url"]
    url = f"{base}/blog/{data['slug']}/"
    nap = cfg.get("nap", {})
    doc = template

    # 1. Marqueur d'idempotence
    doc = re.sub(rf"\n?<!--\s*{re.escape(marker_prefix)}-topic:\s*\d+\s*-->", "", doc)
    doc = sub_once(
        doc, r'<html lang="fr">',
        f'<html lang="fr">\n<!-- {marker_prefix}-topic: {topic["n"]} -->',
        "balise <html lang=\"fr\">",
    )

    # 2. En-tête
    doc = sub_once(doc, r"<title>.*?</title>", f'<title>{esc(data["meta_title"])}</title>', "<title>", re.S)
    doc = sub_once(
        doc, r'<meta name="description" content=".*?" />',
        f'<meta name="description" content="{esca(data["meta_description"])}" />',
        "meta description", re.S,
    )
    doc = sub_once(doc, r'<link rel="canonical" href=".*?" />',
                   f'<link rel="canonical" href="{url}" />', "canonical")

    for prop, value in [
        ("og:title", data["h1"]),
        ("og:description", data["card_excerpt"][:300]),
        ("og:url", url),
        ("article:published_time", date_iso),
        ("article:modified_time", date_iso),
        ("article:section", data["category"]),
    ]:
        doc = sub_once(
            doc, rf'<meta property="{re.escape(prop)}" content=".*?" />',
            f'<meta property="{prop}" content="{esca(value)}" />', f"meta {prop}", re.S,
        )

    for name, value in [
        ("twitter:title", data["meta_title"]),
        ("twitter:description", data["meta_description"]),
    ]:
        doc = sub_once(
            doc, rf'<meta name="{re.escape(name)}" content=".*?" />',
            f'<meta name="{name}" content="{esca(value)}" />', f"meta {name}", re.S,
        )

    # 3. JSON-LD
    address = {
        "@type": "PostalAddress",
        "streetAddress": nap.get("street", ""),
        "postalCode": nap.get("postal_code", ""),
        "addressLocality": nap.get("city", ""),
        "addressRegion": nap.get("region", ""),
        "addressCountry": nap.get("country", "FR"),
    }
    doc = replace_ld(doc, "Article", {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": data["h1"],
        "description": data["meta_description"],
        "inLanguage": "fr-FR",
        "articleSection": data["category"],
        "datePublished": date_iso,
        "dateModified": date_iso,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "url": url,
        "image": {
            "@type": "ImageObject",
            "url": f"{base}/og-belvedere-1200x630.jpg",
            "width": 1200,
            "height": 630,
        },
        "author": {"@type": "Organization", "name": cfg["author"], "url": f"{base}/"},
        "publisher": {
            "@type": "Organization",
            "name": cfg["site_name"],
            "url": f"{base}/",
            "logo": {"@type": "ImageObject", "url": f"{base}/imagesdomaine/logo.png"},
            "telephone": nap.get("phone_e164", ""),
            "email": nap.get("email", ""),
            "address": address,
        },
        "about": [
            {"@type": "Thing", "name": data["category"]},
            {"@type": "Place", "name": "Hautes-Pyrénées"},
            {"@type": "Place", "name": nap.get("city", "")},
        ],
    })

    short = data["h1"] if len(data["h1"]) <= 70 else data["h1"][:67].rsplit(" ", 1)[0] + "…"
    doc = replace_ld(doc, "BreadcrumbList", {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Accueil", "item": f"{base}/"},
            {"@type": "ListItem", "position": 2, "name": "Journal", "item": f"{base}/blog/"},
            {"@type": "ListItem", "position": 3, "name": short, "item": url},
        ],
    })

    doc = replace_ld(doc, "FAQPage", {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": f["question"],
                "acceptedAnswer": {"@type": "Answer", "text": f["answer"]},
            }
            for f in data["faq"]
        ],
    })

    # 4. Fil d'ariane visible
    crumb = short if len(short) <= 60 else short[:57].rsplit(" ", 1)[0] + "…"
    doc, n = re.subn(
        r'(<nav class="breadcrumb wrap" aria-label="Fil d\'ariane">.*?)<li>[^<]*</li>(\s*</ol>)',
        lambda m: m.group(1) + f"<li>{esc(crumb)}</li>" + m.group(2),
        doc, count=1, flags=re.S,
    )
    if n != 1:
        raise ValueError("gabarit : fil d'ariane introuvable")

    # 5. Corps
    body = render_body(data, cfg, date_iso, date_fr)
    doc, n = re.subn(
        r'(<main id="contenu">\n).*?(\n  </main>)',
        lambda m: m.group(1) + body + m.group(2),
        doc, count=1, flags=re.S,
    )
    if n != 1:
        raise ValueError("gabarit : bloc <main id=\"contenu\"> introuvable")

    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Mises à jour des index
# ─────────────────────────────────────────────────────────────────────────────
def update_blog_index(path: Path, data: dict, cfg: dict, date_iso: str, date_fr: str) -> str:
    doc = path.read_text(encoding="utf-8")
    url = f"{cfg['site_url']}/blog/{data['slug']}/"
    href = f"/blog/{data['slug']}/"

    if href in doc:
        log("liste d'articles : carte déjà présente, inchangée")
        return doc

    card = f"""
        <a class="post-card" href="{href}">
          <div class="post-meta">
            <span class="tag">{esc(data['category'])}</span>
            <time datetime="{date_iso}">{date_fr}</time>
            <span>Lecture {data['reading_time']} min</span>
          </div>
          <h2>{esc(data['h1'])}</h2>
          <p>{esc(data['card_excerpt'])}</p>
          <span class="post-more">
            Lire l'article
            <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m9 5 7 7-7 7"/></svg>
          </span>
        </a>
"""
    doc, n = re.subn(
        r'(<div class="post-list">\n)',
        lambda m: m.group(1) + card,
        doc, count=1,
    )
    if n != 1:
        raise ValueError("blog/index.html : conteneur .post-list introuvable")

    # JSON-LD Blog → blogPost
    def add_post(m):
        entry = (
            '{\n      "@type": "BlogPosting",\n'
            f'      "headline": {json.dumps(data["h1"], ensure_ascii=False)},\n'
            f'      "url": "{url}",\n'
            f'      "datePublished": "{date_iso}"\n    }}, '
        )
        return m.group(1) + entry
    doc, n = re.subn(r'("blogPost"\s*:\s*\[)', add_post, doc, count=1)
    if n != 1:
        log("avertissement : tableau blogPost introuvable dans blog/index.html — ignoré")
    return doc


def update_sitemap(path: Path, data: dict, cfg: dict, date_iso: str) -> str:
    doc = path.read_text(encoding="utf-8")
    url = f"{cfg['site_url']}/blog/{data['slug']}/"
    if f"<loc>{url}</loc>" in doc:
        log("sitemap : URL déjà présente, inchangée")
        return doc

    blog_url = f"{cfg['site_url']}/blog/"
    doc = re.sub(
        rf"(<loc>{re.escape(blog_url)}</loc>\s*<lastmod>)[0-9-]+(</lastmod>)",
        lambda m: m.group(1) + date_iso + m.group(2), doc, count=1,
    )

    entry = (
        "  <url>\n"
        f"    <loc>{url}</loc>\n"
        f"    <lastmod>{date_iso}</lastmod>\n"
        "    <changefreq>monthly</changefreq>\n"
        "    <priority>0.7</priority>\n"
        "  </url>\n"
    )
    doc, n = re.subn(
        rf"(<loc>{re.escape(blog_url)}</loc>.*?</url>\n)",
        lambda m: m.group(1) + entry, doc, count=1, flags=re.S,
    )
    if n != 1:
        doc = doc.replace("</urlset>", entry + "</urlset>")
    return doc


def update_rss(path: Path, data: dict, cfg: dict, now: datetime) -> str:
    doc = path.read_text(encoding="utf-8")
    url = f"{cfg['site_url']}/blog/{data['slug']}/"
    if f"<link>{url}</link>" in doc:
        log("rss : item déjà présent, inchangé")
        return doc

    rfc = format_datetime(now)
    doc = re.sub(r"<lastBuildDate>.*?</lastBuildDate>",
                 f"<lastBuildDate>{rfc}</lastBuildDate>", doc, count=1, flags=re.S)

    item = f"""
    <item>
      <title>{esc(data['h1'])}</title>
      <link>{url}</link>
      <guid isPermaLink="true">{url}</guid>
      <pubDate>{rfc}</pubDate>
      <category>{esc(data['category'])}</category>
      <description>{esc(data['card_excerpt'])}</description>
    </item>
"""
    doc, n = re.subn(r"(</image>\n)", lambda m: m.group(1) + item, doc, count=1)
    if n != 1:
        doc = doc.replace("  </channel>", item + "  </channel>")
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Programme principal
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Génère un article de blog et met à jour les index.")
    ap.add_argument("--dry-run", action="store_true", help="génère et affiche sans rien écrire")
    ap.add_argument("--topic", type=int, default=None, help="force le numéro de sujet")
    ap.add_argument("--config", default=str(ROOT / "blog-config.json"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    prefix = site_slug(cfg)
    paths = cfg["paths"]
    blog_dir = ROOT / paths.get("blog_dir", "blog")
    blog_index = ROOT / paths.get("blog_index", "blog/index.html")
    sitemap = ROOT / paths.get("sitemap", "sitemap.xml")
    rss = ROOT / paths.get("rss", "rss.xml")
    workflow_doc = ROOT / paths.get("workflow_doc", "BLOG_WORKFLOW.md")

    for p in (blog_dir, blog_index, sitemap, rss, workflow_doc):
        if not p.exists():
            die(f"chemin attendu introuvable : {p}")

    log(f"site={cfg['site_name']} · marqueur=<!-- {prefix}-topic: N --> · dry-run={args.dry_run}")

    topics = parse_topics(workflow_doc)
    done, slugs, dated = scan_existing(blog_dir, prefix)

    existing_titles = []
    for _, p in dated:
        m = re.search(r"<h1>(.*?)</h1>", p.read_text(encoding="utf-8"), re.S)
        if m:
            existing_titles.append(html_mod.unescape(re.sub(r"\s+", " ", m.group(1)).strip()))

    # Sélection du sujet
    if args.topic is not None:
        topic = next((t for t in topics if t["n"] == args.topic), None)
        if topic is None:
            die(f"sujet n°{args.topic} absent de BLOG_WORKFLOW.md")
        if topic["n"] in done:
            log(f"sujet n°{topic['n']} déjà traité — forcé par --topic")
    else:
        topic = None
        for t in topics:
            if t["n"] in done:
                continue
            if t["published"]:
                log(f"sujet n°{t['n']} ignoré : marqué « publié » dans BLOG_WORKFLOW.md")
                done.add(t["n"])
                continue
            if topic_matches_existing(t, slugs):
                log(f"sujet n°{t['n']} ignoré : un article proche existe déjà")
                done.add(t["n"])
                continue
            topic = t
            break
        if topic is None:
            log("tous les sujets de BLOG_WORKFLOW.md sont traités — rien à publier.")
            log("→ ajoutez de nouvelles lignes au tableau §7 pour relancer la production.")
            return 78

    log(f"sujet retenu : n°{topic['n']} — {topic['subject']}")

    template_path = pick_template(dated)
    template = template_path.read_text(encoding="utf-8")

    system, user = build_prompt(cfg, topic, existing_titles)
    data = call_openai(cfg, system, user)
    try:
        data = validate_payload(data, cfg, topic)
    except ValueError as exc:
        die(f"contenu généré invalide : {exc}")

    log(f"slug={data['slug']} · ~{word_count(data)} mots · "
        f"{len(data['sections'])} sections · {len(data['faq'])} questions · "
        f"description={len(data['meta_description'])} car.")

    target_dir = blog_dir / data["slug"]
    target = target_dir / "index.html"
    if target.exists():
        log(f"ARRÊT PROPRE : {target.relative_to(ROOT)} existe déjà — aucun fichier modifié.")
        return 78

    now = datetime.now(TZ) if TZ else datetime.now()
    date_iso = now.strftime("%Y-%m-%d")
    date_fr = f"{now.day} {MOIS_FR[now.month]} {now.year}"

    try:
        article = render_article(template, data, cfg, topic, prefix, date_iso, date_fr)
        new_index = update_blog_index(blog_index, data, cfg, date_iso, date_fr)
        new_sitemap = update_sitemap(sitemap, data, cfg, date_iso)
        new_rss = update_rss(rss, data, cfg, now)
    except ValueError as exc:
        die(f"rendu impossible : {exc}")

    # Contrôles finaux avant écriture
    for label, needle in [
        ("marqueur d'idempotence", f"{prefix}-topic: {topic['n']}"),
        ("feuille de style blog", "/assets/blog.css"),
        ("canonical", f"{cfg['site_url']}/blog/{data['slug']}/"),
    ]:
        if needle not in article:
            die(f"contrôle final échoué — {label} absent de l'article rendu")
    if article.count("<h1") != 1:
        die(f"contrôle final échoué — {article.count('<h1')} balises h1 (1 attendue)")
    for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', article, re.S):
        try:
            json.loads(m)
        except json.JSONDecodeError as exc:
            die(f"contrôle final échoué — JSON-LD invalide : {exc}")
    log("contrôles finaux : OK (marqueur, canonical, h1 unique, JSON-LD valide)")

    if args.dry_run:
        log("DRY-RUN : aucun fichier écrit.")
        print("\n" + "=" * 72)
        print(f"TITRE      : {data['h1']}")
        print(f"SLUG       : blog/{data['slug']}/")
        print(f"META DESC  : {data['meta_description']} ({len(data['meta_description'])} car.)")
        print(f"CATÉGORIE  : {data['category']}")
        print(f"SECTIONS   : " + " | ".join(s["h2"] for s in data["sections"]))
        print(f"FAQ        :")
        for f in data["faq"]:
            print(f"  - {f['question']}")
        print("=" * 72)
        print("EXTRAIT (200 premiers mots) :\n")
        plain = re.sub(r"<[^>]+>", " ", article.split('<main id="contenu">')[1])
        plain = html_mod.unescape(re.sub(r"\s+", " ", plain)).strip()
        print(" ".join(plain.split()[:200]) + " […]")
        print("=" * 72 + "\n")
        return 0

    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(article, encoding="utf-8")
    blog_index.write_text(new_index, encoding="utf-8")
    sitemap.write_text(new_sitemap, encoding="utf-8")
    rss.write_text(new_rss, encoding="utf-8")

    log(f"écrit : {target.relative_to(ROOT)}")
    log(f"mis à jour : {blog_index.relative_to(ROOT)}, "
        f"{sitemap.relative_to(ROOT)}, {rss.relative_to(ROOT)}")
    log("terminé.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        die("interrompu", 1)
