"""Couleur/motif RÉEL d'une zone RUP -- chantier RUP-seul, consigne du
2026-10-08 (voir la conversation) : une couleur unie doit colorer
directement la cellule d'en-tête de la zone ; un motif complexe (croix,
hachures...) doit recevoir une petite image fidèle au vrai motif, pas
juste rien.

DEUX sources, essayées dans cet ordre :

1. **Légende du document "Grafisch Plan"** -- le PDF cartographique
   officiel propre à CHAQUE dossier RUP (même famille de document que
   "Stedenbouwkundige voorschriften", voir `rup_pdf_service.py`), dont
   l'URL se déduit de `svidlink` : même base, segment
   "Dossierstuk.SV." remplacé par "Dossierstuk.GP." -- confirmé en
   direct (dossier Avelgem). Contrairement à la couche WMS partagée de
   toute la Région (trop générique pour porter la couleur propre à CE
   dossier -- confirmé en direct : le rendu WMS de la zone d'Avelgem ne
   montre AUCUNE couleur), ce PDF contient toujours une légende propre
   au dossier, avec un rectangle REMPLI (couleur unie) ou des TRAITS
   (hachures) juste à gauche du nom de chaque zone -- texte et couleur
   extraits directement de ce document, jamais devinés. Fonctionne
   quel que soit le contenu du champ WFS `legende` (code interne OU
   simple doublon du nom de zone), puisqu'on ne s'appuie plus du tout
   sur ce champ ici mais sur le texte RÉEL de la légende du PDF.
2. **Repli** : requête WMS standard `GetStyles` sur chacune des 3
   couches RUP (`lu_gewrup_gv`/`lu_prorup_gv`/`lu_gemrup_gv`, même host
   Mercator que `wfs_rup_service.py`) -- renvoie le document SLD
   (Styled Layer Descriptor) utilisé par le serveur pour peindre la
   carte régionale : une règle par valeur de `legende` rencontrée
   (`<ogc:PropertyIsEqualTo><ogc:PropertyName>legende</ogc:PropertyName>
   <ogc:Literal>VLAK30000</ogc:Literal>...`), utile seulement pour les
   dossiers dont le champ `legende` est un code interne du style
   partagé -- la voie 1 ci-dessus couvre maintenant ce cas aussi, ce
   repli reste pour les cas où le Grafisch Plan serait indisponible."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import requests
from PIL import Image, ImageDraw

from services.exceptions import ApiServiceError
from services.http_client import HttpClient
from utils.logger import get_logger

_logger = get_logger("services.rup_legende_service")

_WFS_BASE = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ows"
_COUCHES_RUP = ("lu_gemrup_gv", "lu_prorup_gv", "lu_gewrup_gv")  # commune d'abord (de loin la plus fréquente)

_RE_RULE = re.compile(r"<sld:Rule>(.*?)</sld:Rule>", re.S)
_RE_FILTRE_LEGENDE = re.compile(
    r"<ogc:PropertyName>legende</ogc:PropertyName>\s*<ogc:Literal>([^<]*)</ogc:Literal>", re.S,
)
_RE_NOM_MOTIF = re.compile(r"<sld:WellKnownName>([^<]*)</sld:WellKnownName>")
_RE_REMPLISSAGE = re.compile(r'<sld:CssParameter name="fill">#?([0-9A-Fa-f]{6})</sld:CssParameter>')

_TAILLE_IMAGE = 32

# -- Légende du PDF "Grafisch Plan" -----------------------------------------
# La position de la colonne de pastilles de légende VARIE d'un dossier à
# l'autre (voir `_legende_grafisch_plan` -- Avelgem et Glabbeek ont des
# mises en page opposées), donc aucun seuil x0 absolu fixe ne généralise
# -- déduite dynamiquement des formes réellement trouvées dans la bande
# verticale de la légende.
#
# BUG RÉEL trouvé le 2026-10-08 (zone "perimeter woonuitbreidingsgebied",
# Avelgem) : sans borne de LARGEUR sur la colonne de texte, des
# fragments de lettres isolées d'un label de rue tourné (texte vertical
# le long d'une route sur la carte, à x≈2041-2050, très loin à droite --
# confirmé en direct) tombaient dans la même bande verticale que la
# légende et se mélangeaient au nom de zone ("perimeter
# woonuitbreidingsgebied a", "at", "t r"...), cassant la correspondance
# exacte. `_LARGEUR_MAX_COLONNE_TEXTE` borne la largeur de la colonne de
# texte À PARTIR de la position réelle des pastilles (généreuse pour
# couvrir les noms de zone les plus longs rencontrés, ex. "Zone voor
# ontsluiting van KMO zone, woningcluster, autobergplaatsen en langzaam
# verkeer") sans jamais pouvoir atteindre un fragment de la carte
# elle-même, bien plus loin.
_LARGEUR_MAX_COLONNE_TEXTE = 700.0
_TOLERANCE_LIGNE = 4.0
_TOLERANCE_APPARIEMENT = 15.0
_RE_ESPACES = re.compile(r"\s+")


_RE_SEPARATEUR_TIRET = re.compile(r"\s+-\s+")  # " - " SEULEMENT (espaces des 2 côtés) -- jamais le tiret
# collé d'un mot composé néerlandais sans espace (ex. "gemeenschaps-en"), confirmé réel et à préserver.
_RE_DEUX_POINTS = re.compile(r":\s*")
_RE_VIRGULE = re.compile(r"\s*,\s*")
# "Art. 1 ", "Art.1 ", "2.1 " ... en tête de ligne -- la légende du
# Grafisch Plan numérote ses articles (confirmé en direct, dossier
# Glabbeek : "Art. 1 Zone voor recreatie"), jamais présent dans le champ
# WFS `svnaam` correspondant ("Zone voor recreatie" seul).
_RE_NUMERO_ARTICLE = re.compile(r"^(art\.?\s*)?\d+(\.\d+)*\.?\s+", re.IGNORECASE)


def _normaliser(texte: str) -> str:
    """Normalise un nom de zone pour comparer le champ WFS `svnaam` au
    texte réel de la légende du Grafisch Plan -- DEUX sources
    indépendantes pour la même zone, avec de vraies divergences
    confirmées en direct :
    - ponctuation différente (dossier Avelgem RUP_34003_214_00003_00001)
      : "wonen: met X,Y" (légende) contre "wonen:met X - Y" (WFS) pour
      LA MÊME zone ;
    - mot immédiatement dupliqué, typo réelle dans la donnée WFS
      elle-même : "zone zone voor gemengde functies" ;
    - numéro d'article en tête, jamais présent côté WFS (dossier
      Glabbeek) : "Art. 1 Zone voor recreatie" contre "Zone voor
      recreatie".
    Jamais une correspondance approximative sur le CONTENU, seulement
    sur ces variations de forme déjà observées en direct."""
    t = _RE_ESPACES.sub(" ", texte.strip().lower())
    t = _RE_NUMERO_ARTICLE.sub("", t)
    t = _RE_SEPARATEUR_TIRET.sub(",", t)
    t = _RE_DEUX_POINTS.sub(": ", t)
    t = _RE_VIRGULE.sub(",", t)
    mots = t.split(" ")
    mots = [m for i, m in enumerate(mots) if i == 0 or m != mots[i - 1]]
    return " ".join(mots)


def _url_grafisch_plan(svidlink: str) -> Optional[str]:
    """Déduit l'URL du PDF "Grafisch Plan" depuis `svidlink` -- voir le
    docstring du module. `None` si `svidlink` ne suit pas le motif
    attendu (jamais une URL devinée au hasard)."""
    base = svidlink.split("#", 1)[0]
    if "Dossierstuk.SV." not in base:
        return None
    return base.replace("Dossierstuk.SV.", "Dossierstuk.GP.")


def _vers_hex(couleur) -> Optional[str]:
    """Convertit une couleur pdfplumber (niveau de gris, RGB ou CMJN,
    chacun en flottants 0..1) en hex RRGGBB."""
    if couleur is None:
        return None
    if isinstance(couleur, (int, float)):
        v = int(round(couleur * 255))
        return f"{v:02X}{v:02X}{v:02X}"
    if isinstance(couleur, (tuple, list)):
        if len(couleur) == 3:
            r, g, b = couleur
        elif len(couleur) == 4:
            c, m, y, k = couleur
            r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
        else:
            return None
        return f"{int(round(r * 255)):02X}{int(round(g * 255)):02X}{int(round(b * 255)):02X}"
    return None


@dataclass
class StyleLegende:
    couleur_fond: str  # hex SANS '#', ex. "FF0000"
    motif: Optional[str]  # "hachure", nom SLD standard ("cross", "x", ...), None si couleur unie
    couleur_motif: Optional[str]  # hex du motif si DIFFÉRENT du fond (motif bicolore réel)


def _style_depuis_formes(rects: list, lignes: list) -> Optional[StyleLegende]:
    """Une ligne de légende est soit un rectangle REMPLI (couleur
    unie), soit un rectangle NON rempli accompagné de traits colorés
    (hachures) -- jamais les deux confondus, voir le docstring du
    module. Pour les hachures, les VRAIS traits du motif (`lignes`)
    priment sur la couleur du simple contour de la case (`rects` avec
    `fill=False`) -- bug réel trouvé le 2026-10-08 (zone "perimeter
    woonuitbreidingsgebied", Avelgem) : sans cette priorité, la couleur
    grise du cadre de la case (jamais celle du motif réel, toujours
    rouge sur ce document) était choisie en premier. `None` si aucune
    forme exploitable."""
    rect_rempli = next((f for f in rects if f.get("fill") and _vers_hex(f.get("non_stroking_color"))), None)
    if rect_rempli is not None:
        return StyleLegende(couleur_fond=_vers_hex(rect_rempli["non_stroking_color"]), motif=None, couleur_motif=None)
    trait = next(
        (f for f in lignes + rects if _vers_hex(f.get("stroking_color")) not in (None, "000000", "FFFFFF")), None,
    )
    if trait is not None:
        return StyleLegende(couleur_fond="FFFFFF", motif="hachure", couleur_motif=_vers_hex(trait["stroking_color"]))
    return None


_MOTS_DEBUT_LEGENDE = ("zones", "legende")
_MOTS_FIN_LEGENDE = ("overdrukken",)
_HAUTEUR_MAX_SANS_FIN = 500.0  # si aucun titre de fin trouvé, borne raisonnable -- jamais la page entière
_MARGE_COLONNE_SWATCH = 5.0
_LARGEUR_MAX_COLONNE_SWATCH = 150.0  # garde-fou : des pastilles de légende sont toujours étroites et alignées


def _legende_grafisch_plan(pdf) -> Dict[str, StyleLegende]:
    """Analyse la légende du Grafisch Plan : associe chaque nom de zone
    (texte normalisé) à la couleur/motif de la forme juste à sa gauche,
    même ligne.

    BUG RÉEL trouvé le 2026-10-12 (dossier Glabbeek) : certains documents
    simulent le gras en dessinant chaque caractère (et chaque pastille de
    légende) DEUX FOIS à la position EXACTE -- confirmé en direct au
    niveau caractère -- ce qui cassait toute extraction de texte avant
    `page.dedupe_chars()` (méthode native pdfplumber faite pour ça).

    BUG RÉEL #2 : deux dossiers différents utilisent des titres de
    légende ("zones" vs "Legende") ET des mises en page OPPOSÉES
    (Avelgem : pastilles À GAUCHE du texte, alignées sur une position
    fixe ; Glabbeek : pastille alignée SOUS le titre "Legende" lui-même,
    texte juste à droite) -- aucune constante de colonne absolue ne
    généralise. La colonne des pastilles est maintenant déduite des
    VRAIES positions des formes (rectangles/traits) trouvées dans la
    bande verticale de la légende, jamais d'un seuil fixe -- le texte
    est tout ce qui commence juste après leur bord droit."""
    resultat: Dict[str, StyleLegende] = {}
    for page_brute in pdf.pages:
        page = page_brute.dedupe_chars()
        mots = page.extract_words()
        debut = next((w for w in mots if w["text"].lower() in _MOTS_DEBUT_LEGENDE), None)
        if debut is None:
            continue
        i_debut = debut["top"]
        fin = next((w for w in mots if w["text"].lower() in _MOTS_FIN_LEGENDE and w["top"] > i_debut), None)
        borne_fin = fin["top"] if fin is not None else i_debut + _HAUTEUR_MAX_SANS_FIN

        rects_bande = [f for f in page.rects if i_debut < f.get("top", -1) < borne_fin]
        lignes_bande = [f for f in page.lines if i_debut < f.get("top", -1) < borne_fin]
        if not rects_bande:
            continue
        # Colonne des pastilles déduite des RECTANGLES seulement (chaque
        # entrée de légende, même hachurée, a toujours un rectangle --
        # au moins son contour -- confirmé en direct sur 2 mises en page
        # différentes). PAS des `lignes` : bug réel trouvé le 2026-10-12
        # (dossier Avelgem) -- `page.lines` contient aussi des dizaines
        # de petits traits isolés dispersés sur toute la largeur de la
        # carte (tirets de texte, amorces de cotation...), rien à voir
        # avec la légende, qui auraient fait rejeter la page entière.
        #
        # BUG RÉEL #2 (même jour, dossier Glabbeek) : même parmi les
        # RECTANGLES, un seul rectangle isolé sans rapport avec la
        # légende (ex. un cadre de texte voisin, bien plus large que les
        # pastilles) suffit à fausser un simple min/max -- la colonne
        # est donc déduite de la position x0 la PLUS FRÉQUENTE parmi les
        # rectangles de la bande (les vraies pastilles sont répétées à
        # l'identique, jamais un unique rectangle isolé).
        compte_x0 = Counter(round(f["x0"], 1) for f in rects_bande)
        x0_principal, _ = compte_x0.most_common(1)[0]
        rects_pastille = [f for f in rects_bande if abs(f["x0"] - x0_principal) < 2.0]
        x0_min_swatch = min(f["x0"] for f in rects_pastille)
        x1_max_swatch = max(f.get("x1", f["x0"]) for f in rects_pastille)
        if x1_max_swatch - x0_min_swatch > _LARGEUR_MAX_COLONNE_SWATCH:
            continue  # rectangles trop dispersés pour être une simple colonne de pastilles -- jamais un découpage incertain

        col_texte_min = x1_max_swatch - _MARGE_COLONNE_SWATCH
        col_texte_max = x1_max_swatch + _LARGEUR_MAX_COLONNE_TEXTE
        lignes_texte: Dict[float, list] = {}
        for w in mots:
            if not (i_debut < w["top"] < borne_fin) or not (col_texte_min <= w["x0"] < col_texte_max):
                continue
            cle = next((k for k in lignes_texte if abs(k - w["top"]) < _TOLERANCE_LIGNE), w["top"])
            lignes_texte.setdefault(cle, []).append(w)

        # Même principe pour le choix de la forme d'UNE ligne précise :
        # restreint à la colonne des pastilles (x0_min_swatch/x1_max_swatch),
        # jamais juste "proche en hauteur" -- bug réel trouvé le
        # 2026-10-12 (zone "perimeter woonuitbreidingsgebied", Avelgem) :
        # un trait gris isolé à x≈3028 (tout à droite de la carte) tombait
        # par hasard à moins de 15pt de hauteur de cette ligne de légende
        # et était choisi À LA PLACE du vrai trait rouge du motif, à
        # x≈680 (la colonne des pastilles).
        col_swatch_min, col_swatch_max = x0_min_swatch - _MARGE_COLONNE_SWATCH, x1_max_swatch + _MARGE_COLONNE_SWATCH
        rects_bande_col = [f for f in rects_bande if col_swatch_min <= f["x0"] < col_swatch_max]
        lignes_bande_col = [f for f in lignes_bande if col_swatch_min <= f["x0"] < col_swatch_max]

        for top, mots_ligne in lignes_texte.items():
            texte_norm = _normaliser(" ".join(w["text"] for w in sorted(mots_ligne, key=lambda w: w["x0"])))
            if not texte_norm:
                continue
            rects_voisins = [f for f in rects_bande_col if abs(f["top"] - top) < _TOLERANCE_APPARIEMENT]
            lignes_voisines = [f for f in lignes_bande_col if abs(f["top"] - top) < _TOLERANCE_APPARIEMENT]
            style = _style_depuis_formes(rects_voisins, lignes_voisines)
            if style is not None:
                resultat[texte_norm] = style
    return resultat


class RupLegendeService:
    def __init__(self, http: HttpClient, cache_dir: Path) -> None:
        self._http = http
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._styles: Dict[str, Dict[str, StyleLegende]] = {}
        self._legendes_grafisch_plan: Dict[str, Dict[str, StyleLegende]] = {}  # url -> {nom_zone_normalisé: style}

    def couleur_depuis_grafisch_plan(self, svidlink: str, svnaam: str) -> Optional[StyleLegende]:
        """Cherche `svnaam` dans la légende du VRAI document "Grafisch
        Plan" du dossier (déduit de `svidlink`, voir le docstring du
        module) -- `None` si le lien ne suit pas le motif attendu, le
        téléchargement échoue, ou AUCUNE ligne de légende ne
        correspond exactement (normalisée) à `svnaam` -- jamais une
        correspondance approximative."""
        url = _url_grafisch_plan(svidlink)
        if url is None:
            return None
        chemin = self._telecharger(url)
        if chemin is None:
            return None
        if url not in self._legendes_grafisch_plan:
            try:
                import pdfplumber
                with pdfplumber.open(chemin) as pdf:
                    self._legendes_grafisch_plan[url] = _legende_grafisch_plan(pdf)
            except Exception as exc:  # noqa: BLE001 -- un Grafisch Plan illisible ne doit jamais faire échouer le traitement de la parcelle
                _logger.warning("Légende du Grafisch Plan illisible (%s) : %s", url, exc)
                self._legendes_grafisch_plan[url] = {}
        return self._legendes_grafisch_plan[url].get(_normaliser(svnaam))

    def _telecharger(self, url: str) -> Optional[Path]:
        nom = re.sub(r"[^A-Za-z0-9_.-]", "_", url.rsplit("/", 1)[-1]) or "document"
        chemin = self._cache_dir / f"{nom}.pdf"
        if chemin.exists():
            return chemin
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except requests.exceptions.RequestException as exc:
            _logger.warning("Téléchargement du Grafisch Plan échoué (%s) : %s", url, exc)
            return None
        chemin_tmp = chemin.with_suffix(".pdf.tmp")
        chemin_tmp.write_bytes(r.content)
        chemin_tmp.replace(chemin)
        return chemin

    def _charger_style(self, couche: str) -> Dict[str, StyleLegende]:
        if couche in self._styles:
            return self._styles[couche]
        style: Dict[str, StyleLegende] = {}
        try:
            xml = self._http.get_text(
                _WFS_BASE,
                {"service": "WMS", "version": "1.1.1", "request": "GetStyles", "layers": couche},
                service_key="rup_legende",
            )
        except Exception as exc:  # noqa: BLE001 -- une légende indisponible ne doit jamais faire échouer le traitement de la parcelle
            _logger.warning("Style SLD de '%s' indisponible : %s", couche, exc)
            self._styles[couche] = style
            return style
        for bloc in _RE_RULE.findall(xml):
            m_legende = _RE_FILTRE_LEGENDE.search(bloc)
            if not m_legende:
                continue
            legende = m_legende.group(1).strip()
            remplissages = _RE_REMPLISSAGE.findall(bloc)
            if not remplissages:
                continue
            couleur_fond = remplissages[0].upper()
            m_motif = _RE_NOM_MOTIF.search(bloc)
            motif = m_motif.group(1).strip() if m_motif else None
            couleur_motif = None
            if motif:
                autres = {c.upper() for c in remplissages[1:]} - {couleur_fond}
                if autres:
                    couleur_motif = sorted(autres)[0]
            style[legende] = StyleLegende(couleur_fond=couleur_fond, motif=motif, couleur_motif=couleur_motif)
        self._styles[couche] = style
        _logger.info("Style SLD de '%s' : %d règle(s) de légende chargée(s).", couche, len(style))
        return style

    def style_pour_legende(self, legende: str) -> Optional[StyleLegende]:
        """Cherche `legende` dans les 3 couches RUP (commune d'abord).
        `None` si introuvable dans AUCUNE (voir la limite réelle décrite
        dans le docstring du module -- jamais deviné)."""
        if not legende:
            return None
        for couche in _COUCHES_RUP:
            style = self._charger_style(couche).get(legende)
            if style is not None:
                return style
        return None

    def generer_image_motif(self, style: StyleLegende) -> Optional[Path]:
        """Génère (et met en cache disque, réutilisée pour toute zone
        partageant le même motif/couleurs) une petite image PNG fidèle
        au VRAI motif SLD de `style` -- `None` si `style.motif` est
        `None` (couleur unie, pas besoin d'image, voir
        `excel_service_rup.py` qui colore alors directement la
        cellule)."""
        if style.motif is None:
            return None
        nom = f"motif_{style.couleur_fond}_{style.motif}_{style.couleur_motif or 'seul'}.png"
        chemin = self._cache_dir / nom
        if chemin.exists():
            return chemin
        img = Image.new("RGB", (_TAILLE_IMAGE, _TAILLE_IMAGE), f"#{style.couleur_fond}")
        draw = ImageDraw.Draw(img)
        couleur_trait = f"#{style.couleur_motif}" if style.couleur_motif else "#000000"
        _dessiner_motif(draw, style.motif, _TAILLE_IMAGE, couleur_trait)
        img.save(chemin)
        return chemin


def _dessiner_motif(draw: "ImageDraw.ImageDraw", motif: str, taille: int, couleur: str) -> None:
    """Reproduit la FORME déclarée par le motif SLD (`sld:WellKnownName`,
    vocabulaire standard OGC -- cross/x/slash/backslash/circle/square/
    triangle), pas une approximation inventée : le nom et les couleurs
    viennent directement de la règle officielle, seul le tracé exact au
    pixel près est une reproduction raisonnable (taille de vignette
    Excel, pas besoin du rendu pixel-parfait du serveur de carte)."""
    m = motif.lower()
    pas = max(taille // 4, 4)
    centres = [(x, y) for x in range(pas, taille, pas * 2) for y in range(pas, taille, pas * 2)]
    if m == "hachure":
        # Trait détecté dans la légende du Grafisch Plan (voir
        # _style_depuis_formes) sans forme précise connue -- hachures
        # diagonales simples, motif générique mais jamais une couleur
        # inventée (celle-ci vient bien du vrai document).
        for i in range(-taille, taille, max(pas // 2, 3)):
            draw.line([(i, taille), (i + taille, 0)], fill=couleur, width=1)
    elif m in ("cross", "plus"):
        for x, y in centres:
            draw.line([(x - pas // 2, y), (x + pas // 2, y)], fill=couleur, width=2)
            draw.line([(x, y - pas // 2), (x, y + pas // 2)], fill=couleur, width=2)
    elif m == "x":
        for x, y in centres:
            draw.line([(x - pas // 2, y - pas // 2), (x + pas // 2, y + pas // 2)], fill=couleur, width=2)
            draw.line([(x - pas // 2, y + pas // 2), (x + pas // 2, y - pas // 2)], fill=couleur, width=2)
    elif m == "slash":
        for i in range(-taille, taille, pas):
            draw.line([(i, taille), (i + taille, 0)], fill=couleur, width=2)
    elif m == "backslash":
        for i in range(-taille, taille, pas):
            draw.line([(i, 0), (i + taille, taille)], fill=couleur, width=2)
    elif m == "circle":
        for x, y in centres:
            r = pas // 3
            draw.ellipse([x - r, y - r, x + r, y + r], outline=couleur, width=2)
    elif m == "square":
        for x, y in centres:
            r = pas // 3
            draw.rectangle([x - r, y - r, x + r, y + r], outline=couleur, width=2)
    elif m == "triangle":
        for x, y in centres:
            r = pas // 3
            draw.polygon([(x, y - r), (x - r, y + r), (x + r, y + r)], outline=couleur, width=2)
    # Motif SLD rare/inconnu (ex. "star") : pas de forme devinée, la
    # couleur de fond seule (déjà posée par generer_image_motif) reste
    # une information réelle et utile, jamais une forme inventée.
