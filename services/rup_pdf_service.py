"""Extraction du texte légal "Bestemming" depuis le PDF des
"Stedenbouwkundige voorschriften" d'un RUP -- chantier RUP-seul du
2026-10-07 (voir le plan). Point d'entrée : `InfoRup.svidlink`
(`services/wfs_rup_service.py`), ex.
`https://download.dsi.omgeving.vlaanderen.be/....SV.1_1#page=10` -- la
partie avant `#` est l'URL du PDF complet, le fragment `#page=N` la
page (1-indexée, convention des lecteurs PDF) où démarre l'article de
CETTE zone précise.

PIÈGE RÉEL confirmé en direct sur DEUX dossiers RUP différents
(Aartselaar, `RUP_11001_214_00005_00001`, page 10 ; Avelgem,
`RUP_34003_214_00005_00001`, page 6) : le document est en **deux
colonnes** -- gauche "...voorschrift(en)" (texte réglementaire, celui
qu'on veut), droite "Toelichting" (commentaire non contraignant, à
IGNORER). Un simple `page.extract_text()` entrelace les deux colonnes
ligne par ligne dans un ordre de lecture incorrect.

PIÈGE RÉEL #2 (découvert en comparant ces deux dossiers, PAS visible
sur un seul) : chaque dossier RUP est produit par un bureau d'études
différent -- AUCUNE mise en page commune entre deux PDF de dossiers
différents. Largeur de page différente (841pt vs 1191pt, donc la
position `x0` de la frontière entre colonnes n'est PAS une constante
absolue), police/taille des titres différentes (Aartselaar :
Calibri-Bold 12pt pour "Bestemming" seul ; Avelgem : Verdana-Bold 9pt
--- MÊME taille que le corps du texte --- pour "Bestemming" précédé
d'une numérotation NON grasse "2.1 "). Toute constante absolue
(position de colonne, taille de police) calibrée sur UN SEUL exemplaire
ne généralise PAS -- confirmé par un vrai faux négatif en direct
(section "Bestemming" présente mais jamais trouvée) avant cette
réécriture. Approche robuste retenue, sans aucune constante absolue :
- **Frontière de colonne** : le plus grand écart horizontal entre deux
  mots consécutifs (triés par `x0`) dans la moitié centrale de la page
  (25%-75% de sa largeur) -- un document en 2 colonnes a TOUJOURS un
  vrai "couloir" vide entre elles, quelle que soit la largeur de page.
  Recalculé À CHAQUE PAGE (pas de constante figée).
- **Détection d'un titre** : une ligne (colonne de gauche) avec AU MOINS
  UN mot en gras ET au plus 8 mots au total -- capte "Bestemming" seul
  (Aartselaar) comme "2.1 Bestemming" (Avelgem, où seul "Bestemming"
  est gras, pas "2.1") ou "2 Zone voor ... nevenfuncties" (titre
  d'article), sans dépendre d'une taille de police précise. Toujours
  ignorer la ligne d'en-tête de colonne répétée en haut de chaque page
  (détectée par le mot "voorschrift" qu'elle contient toujours, dans
  les deux variantes vues : "Verordend stedenbouwkundig voorschrift" /
  "VERORDENENDE VOORSCHRIFTEN").

La section "Bestemming" n'est PAS forcément libellée exactement
"Bestemming" (variantes réelles vues dans `Exemplaire.xlsx` :
"Bestemming en bebouwing", "Bestemmingsvoorschriften", "2.1
Bestemming") -- on cherche le premier titre dont le texte, une fois une
éventuelle numérotation ("2.1 ", "3.", ...) retirée, commence par
"bestemming" (insensible à la casse), et on capture tout jusqu'au
PROCHAIN titre rencontré (qui peut être sur la page suivante -- voir le
cas réel Aartselaar où la section continue jusqu'à juste avant
"Inrichting" en haut de la page suivante) -- jamais une distance de
page fixe, toujours le prochain titre réel."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

import pdfplumber
import requests

from utils.logger import get_logger

_logger = get_logger("services.rup_pdf_service")

_TOLERANCE_MEME_LIGNE = 2.0
_MAX_MOTS_TITRE = 8
_MAX_PAGES_RECHERCHE_BESTEMMING = 3  # jamais un parcours illimité du document si "Bestemming" n'apparaît pas

_RE_PAGE_FRAGMENT = re.compile(r"page=(\d+)")
_RE_NUMEROTATION = re.compile(r"^[\d]+(\.[\d]+)*\.?\s+")


class RupPdfService:
    def __init__(self, cache_dir: Path, timeout: float = 30.0) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout

    def extraire_bestemming(self, svidlink: str) -> Optional[str]:
        """Renvoie le texte "Bestemming..." complet pour la zone visée
        par `svidlink`, ou `None` (jamais deviné/tronqué) si le lien est
        sans fragment de page, le téléchargement échoue, ou aucune
        section "Bestemming" n'est trouvée à proximité de la page
        indiquée."""
        url_pdf, page_1_based = self._decouper_svidlink(svidlink)
        if page_1_based is None:
            _logger.warning("svidlink sans fragment '#page=N' exploitable : %s", svidlink)
            return None
        chemin_pdf = self._telecharger(url_pdf)
        if chemin_pdf is None:
            return None
        try:
            with pdfplumber.open(chemin_pdf) as pdf:
                return self._extraire_depuis_page(pdf, page_1_based - 1)
        except Exception as exc:  # noqa: BLE001 -- un PDF illisible ne doit jamais faire échouer tout le traitement de la parcelle
            _logger.warning("Extraction PDF échouée (%s, page %d) : %s", url_pdf, page_1_based, exc)
            return None

    @staticmethod
    def _decouper_svidlink(svidlink: str) -> Tuple[str, Optional[int]]:
        if "#" not in svidlink:
            return svidlink, None
        base, frag = svidlink.split("#", 1)
        m = _RE_PAGE_FRAGMENT.search(frag)
        return base, (int(m.group(1)) if m else None)

    def _telecharger(self, url_pdf: str) -> Optional[Path]:
        nom = re.sub(r"[^A-Za-z0-9_.-]", "_", url_pdf.rsplit("/", 1)[-1]) or "document"
        chemin = self._cache_dir / f"{nom}.pdf"
        if chemin.exists():
            return chemin
        try:
            r = requests.get(url_pdf, timeout=self._timeout)
            r.raise_for_status()
        except requests.exceptions.RequestException as exc:
            _logger.warning("Téléchargement PDF échoué (%s) : %s", url_pdf, exc)
            return None
        chemin_tmp = chemin.with_suffix(".pdf.tmp")
        chemin_tmp.write_bytes(r.content)
        chemin_tmp.replace(chemin)
        return chemin

    @staticmethod
    def _frontiere_colonnes(mots: list) -> Optional[float]:
        """Le plus grand écart horizontal entre deux mots consécutifs
        (triés par `x0`) dans la moitié centrale de la page -- voir le
        docstring du module. `None` si la page ne semble pas avoir 2
        colonnes (aucun mot dans cette zone, page quasi vide)."""
        if not mots:
            return None
        largeur = max(w["x1"] for w in mots)
        bas, haut = largeur * 0.25, largeur * 0.75
        xs = sorted(w["x0"] for w in mots)
        meilleur_ecart, meilleure_frontiere = 0.0, None
        for a, b in zip(xs, xs[1:]):
            if bas <= a <= haut or bas <= b <= haut:
                if b - a > meilleur_ecart:
                    meilleur_ecart, meilleure_frontiere = b - a, (a + b) / 2
        return meilleure_frontiere

    @classmethod
    def _lignes_colonne_gauche(cls, page) -> List[Tuple[str, bool]]:
        """Lignes (texte, est_titre) de la colonne de GAUCHE seulement,
        dans l'ordre de lecture -- voir le docstring du module pour la
        détection de la frontière de colonne et des titres."""
        mots = page.extract_words(extra_attrs=["fontname", "size"])
        frontiere = cls._frontiere_colonnes(mots)
        gauche = [w for w in mots if frontiere is None or w["x0"] < frontiere]
        lignes: dict = {}
        for w in gauche:
            cle = next((k for k in lignes if abs(k - w["top"]) < _TOLERANCE_MEME_LIGNE), w["top"])
            lignes.setdefault(cle, []).append(w)
        resultat: List[Tuple[str, bool]] = []
        for top in sorted(lignes):
            mots_ligne = sorted(lignes[top], key=lambda w: w["x0"])
            texte = " ".join(w["text"] for w in mots_ligne)
            if "voorschrift" in texte.lower():
                continue  # en-tête de colonne répété en haut de chaque page, jamais du contenu
            est_titre = len(mots_ligne) <= _MAX_MOTS_TITRE and any("Bold" in w["fontname"] for w in mots_ligne)
            resultat.append((texte, est_titre))
        return resultat

    def _extraire_depuis_page(self, pdf, index_page_depart: int) -> Optional[str]:
        capture = False
        morceaux: List[str] = []
        derniere_page_scannee = index_page_depart
        for index_page in range(index_page_depart, len(pdf.pages)):
            derniere_page_scannee = index_page
            for texte, est_titre in self._lignes_colonne_gauche(pdf.pages[index_page]):
                if est_titre:
                    if capture:
                        return "\n".join(morceaux).strip()
                    if _RE_NUMEROTATION.sub("", texte.strip()).lower().startswith("bestemming"):
                        capture = True
                        morceaux = [texte.strip()]
                    continue
                if capture:
                    morceaux.append(texte.strip())
            if not capture and index_page - index_page_depart >= _MAX_PAGES_RECHERCHE_BESTEMMING:
                break
        if capture:
            return "\n".join(morceaux).strip()
        _logger.warning(
            "Aucune section 'Bestemming' trouvée entre les pages %d et %d.",
            index_page_depart + 1, derniere_page_scannee + 1,
        )
        return None
