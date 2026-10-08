"""Couleur/motif RÉEL d'une zone RUP -- chantier RUP-seul, consigne du
2026-10-08 (voir la conversation) : une couleur unie doit colorer
directement la cellule d'en-tête de la zone ; un motif complexe (croix,
hachures...) doit recevoir une petite image fidèle au vrai motif, pas
juste rien.

Source : requête WMS standard `GetStyles` sur chacune des 3 couches RUP
(`lu_gewrup_gv`/`lu_prorup_gv`/`lu_gemrup_gv`, même host Mercator que
`wfs_rup_service.py`) -- renvoie le document SLD (Styled Layer
Descriptor) utilisé par le serveur pour PEINDRE la carte elle-même :
une règle par valeur de `legende` rencontrée
(`<ogc:PropertyIsEqualTo><ogc:PropertyName>legende</ogc:PropertyName>
<ogc:Literal>VLAK30000</ogc:Literal>...`), avec sa VRAIE couleur
(`<sld:CssParameter name="fill">#FF0000</sld:CssParameter>`) et, si la
zone est hachurée plutôt qu'unie, le nom du motif SLD standard
(`<sld:WellKnownName>cross</sld:WellKnownName>`, etc.) -- jamais une
couleur devinée depuis le texte libre, toujours la définition
AUTORITATIVE réellement utilisée par le rendu de la carte.

LIMITE RÉELLE confirmée en direct (dossier RUP d'Avelgem,
`RUP_34003_214_00005_00001`) : certains dossiers RUP remplissent le
champ `legende` avec un simple doublon du nom de la zone (ex. "zone
voor wonen met beperkte nevenfuncties") plutôt qu'un code interne du
style partagé (`VLAKxxxxx`) -- dans ce cas, AUCUNE règle du style ne
filtre sur cette valeur, et aucune couleur n'est récupérable par cette
voie (ni par l'ancienne heuristique texte de `excel_service.py`,
reconduite ici en repli). Jamais une couleur inventée dans ce cas --
la cellule reste simplement sans couleur, comme avant ce chantier."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

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


@dataclass
class StyleLegende:
    couleur_fond: str  # hex SANS '#', ex. "FF0000"
    motif: Optional[str]  # nom SLD standard ("cross", "x", "slash", ...), None si couleur unie
    couleur_motif: Optional[str]  # hex du motif si DIFFÉRENT du fond (motif bicolore réel)


class RupLegendeService:
    def __init__(self, http: HttpClient, cache_dir: Path) -> None:
        self._http = http
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._styles: Dict[str, Dict[str, StyleLegende]] = {}

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
    if m in ("cross", "plus"):
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
