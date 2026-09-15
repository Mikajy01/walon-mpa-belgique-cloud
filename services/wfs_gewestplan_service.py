"""Accès au WFS Gewestplan (plan de secteur) — Mercator/Departement
Omgeving. Confirmé en direct (2026-09-16, après la panne serveur des
jours précédents) : `https://www.mercator.vlaanderen.be/
raadpleegdienstenmercatorpubliek/ows`, couche vecteur **`lu:lu_gwp_gv`**
(PAS `lu_gwp_rv_raster`, une image).

PIÈGE RÉEL confirmé en direct : ce service utilise **EPSG:3812**
(Lambert Belge 2008), PAS EPSG:31370 (Lambert 72) comme le reste des
services flamands déjà câblés (watertoets, économie, landinrichting,
woningbouw, landschap) — vérifié via `srsName` dans la géométrie
renvoyée. Nécessite une reprojection depuis le point d'adresse (Lambert
72) via `pyproj`.

Champ de catégorie confirmé : `lu:svnaam` (texte, ex. "woongebieden met
landelijk karakter") — correspond au libellé de la colonne Excel
concernée, à comparer via `utils.text_normalize.normaliser` (le gabarit
contient au moins une faute de frappe connue, "culturelke" au lieu de
"cultureel", confirmée en direct sur un vrai retour d'API)."""

from __future__ import annotations

import re
from typing import List, Optional

import pyproj

import config
from services.http_client import HttpClient
from utils.logger import get_logger

_logger = get_logger("services.wfs_gewestplan_service")

_WFS_BASE = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ows"
_EPSG_MERCATOR = "EPSG:3812"

_transformer_l72_vers_3812 = pyproj.Transformer.from_crs("EPSG:31370", _EPSG_MERCATOR, always_xy=True)


def lambert72_vers_3812(x: float, y: float) -> tuple[float, float]:
    return _transformer_l72_vers_3812.transform(x, y)


class WfsGewestplanService:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @staticmethod
    def _bbox(x: float, y: float, marge_m: float = 5.0) -> str:
        return f"{x - marge_m},{y - marge_m},{x + marge_m},{y + marge_m},urn:ogc:def:crs:EPSG::3812"

    def svnaam_categories(self, x_l72: float, y_l72: float, marge_m: float = 5.0) -> List[str]:
        """Renvoie la/les valeur(s) `svnaam` trouvée(s) au point donné
        (coordonnées Lambert 72, reprojetées en interne) — normalement 1
        seule, mais renvoie TOUTES celles trouvées (jamais deviné laquelle
        choisir) si plusieurs zones se superposent à ce point précis."""
        x, y = lambert72_vers_3812(x_l72, y_l72)
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "lu:lu_gwp_gv", "count": 10,
            "BBOX": self._bbox(x, y, marge_m),
        }
        try:
            xml = self._http.get_text(_WFS_BASE, params, service_key="gewestplan_be")
        except Exception as exc:  # noqa: BLE001 — une couche indisponible ne doit jamais faire échouer tout le traitement de la parcelle
            _logger.warning("Gewestplan indisponible (x=%s, y=%s) : %s", x_l72, y_l72, exc)
            return []
        noms = re.findall(r"<lu:svnaam>([^<]*)</lu:svnaam>", xml)
        return [n.strip() for n in noms]
