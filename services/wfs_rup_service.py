"""Accès aux 3 WFS RUP (Ruimtelijke UitvoeringsPlannen — plans
d'exécution spatiaux), host Mercator/EPSG:3812.

PIÈGE RÉEL confirmé en direct : le nom de couche du niveau PROVINCE
n'est PAS `lu:lu_provrup_gv` (supposé initialement, jamais vérifié) mais
**`lu:lu_prorup_gv`** ("prorup", pas "provrup") — confirmé via la liste
réelle des `FeatureType` du `GetCapabilities`.

Décision explicite de l'utilisateur (2026-09-16) : plutôt que de
classer chaque parcelle dans une des ~25 catégories détaillées du bloc
RUP de la feuille principale (jamais construit, risque de classement
faux — voir le plan), on remplit directement la feuille "RUP" dédiée
avec ce que l'API donne déjà SANS AUCUN classement : `fichelink` —
l'URL exacte demandée par l'instruction du gabarit ("indiquer le lien
vers le RUP"), littéralement
`https://dsi.omgeving.vlaanderen.be/fiche-detail/<uuid>`, confirmée en
direct dans la réponse WFS.

CORRECTION du 2026-09-17 (comparaison avec un fichier traité
manuellement par un collègue, ex. "RUP_11025_214_00001_00001") : la
colonne "RUP" de la feuille dédiée n'attend PAS le nom lisible du plan
(`naam`/`svnaam`, ex. "Binnenstad — woonzone met tuinstrook") mais le
**code officiel `algplanid`** du plan, ex. `RUP_71053_214_00026_00026`
(71053 = code NIS de Sint-Truiden, confirmé en direct) — présent tel
quel dans la réponse WFS des 3 niveaux (vérifié en direct sur
`lu_gemrup_gv`/`lu_prorup_gv`, et présent dans le schéma de
`lu_gewrup_gv` même si aucune feature régionale n'a encore été
rencontrée pour le tester). `naam`/`svnaam` restent extraits et
disponibles (utiles pour un contexte humain) mais ne sont plus ce qui
est écrit dans la feuille Excel."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from services.http_client import HttpClient
from services.wfs_gewestplan_service import lambert72_vers_3812
from utils.logger import get_logger

_logger = get_logger("services.wfs_rup_service")

_WFS_BASE = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ows"


@dataclass
class InfoRup:
    algplanid: str  # code officiel du plan, ex. "RUP_71053_214_00026_00026"
    naam: str  # nom du plan RUP, ex. "Binnenstad" (contexte humain, pas écrit dans l'Excel)
    svnaam: str  # nom de la zone précise, ex. "woonzone met tuinstrook" (idem)
    fichelink: str  # URL exacte vers la fiche RUP (dsi.omgeving.vlaanderen.be)


class WfsRupService:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @staticmethod
    def _bbox(x: float, y: float, marge_m: float = 5.0) -> str:
        return f"{x - marge_m},{y - marge_m},{x + marge_m},{y + marge_m},urn:ogc:def:crs:EPSG::3812"

    def _info(self, layer: str, x_l72: float, y_l72: float, marge_m: float = 5.0) -> List[InfoRup]:
        x, y = lambert72_vers_3812(x_l72, y_l72)
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"lu:{layer}", "count": 10,
            "BBOX": self._bbox(x, y, marge_m),
        }
        try:
            xml = self._http.get_text(_WFS_BASE, params, service_key="rup_be")
        except Exception as exc:  # noqa: BLE001 — une couche indisponible ne doit jamais faire échouer tout le traitement de la parcelle
            _logger.warning("Couche '%s' indisponible (x=%s, y=%s) : %s", layer, x_l72, y_l72, exc)
            return []
        resultats: List[InfoRup] = []
        for bloc in xml.split("<wfs:member>")[1:]:
            m_algplanid = re.search(r"<lu:algplanid>([^<]*)</lu:algplanid>", bloc)
            m_naam = re.search(r"<lu:naam>([^<]*)</lu:naam>", bloc)
            m_sv = re.search(r"<lu:svnaam>([^<]*)</lu:svnaam>", bloc)
            m_lien = re.search(r"<lu:fichelink>([^<]*)</lu:fichelink>", bloc)
            if m_lien is None or m_algplanid is None:
                _logger.warning("Couche '%s' : membre sans 'fichelink'/'algplanid' exploitable, ignoré.", layer)
                continue
            resultats.append(InfoRup(
                algplanid=m_algplanid.group(1).strip(),
                naam=(m_naam.group(1).strip() if m_naam else ""),
                svnaam=(m_sv.group(1).strip() if m_sv else ""),
                fichelink=m_lien.group(1).strip(),
            ))
        return resultats

    def rup_region(self, x: float, y: float) -> List[InfoRup]:
        return self._info("lu_gewrup_gv", x, y)

    def rup_province(self, x: float, y: float) -> List[InfoRup]:
        return self._info("lu_prorup_gv", x, y)

    def rup_commune(self, x: float, y: float) -> List[InfoRup]:
        return self._info("lu_gemrup_gv", x, y)
