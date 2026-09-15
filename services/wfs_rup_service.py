"""Accès aux 3 WFS RUP (Ruimtelijke UitvoeringsPlannen — plans
d'exécution spatiaux), même host Mercator/EPSG:3812 que
`wfs_gewestplan_service.py`, même champ `svnaam`.

PIÈGE RÉEL confirmé en direct : le nom de couche du niveau PROVINCE
n'est PAS `lu:lu_provrup_gv` (ce que la recherche initiale avait
supposé, jamais vérifié) mais **`lu:lu_prorup_gv`** ("prorup", pas
"provrup") — confirmé via la liste réelle des `FeatureType` du
`GetCapabilities` (`lu_provrup_gv` renvoie une exception WFS
"Feature type ... unknown"). Toujours vérifier le nom exact plutôt que
le déduire du niveau régional/communal."""

from __future__ import annotations

import re
from typing import List

from services.http_client import HttpClient
from services.wfs_gewestplan_service import lambert72_vers_3812
from utils.logger import get_logger

_logger = get_logger("services.wfs_rup_service")

_WFS_BASE = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ows"


class WfsRupService:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @staticmethod
    def _bbox(x: float, y: float, marge_m: float = 5.0) -> str:
        return f"{x - marge_m},{y - marge_m},{x + marge_m},{y + marge_m},urn:ogc:def:crs:EPSG::3812"

    def _svnaam_categories(self, layer: str, x_l72: float, y_l72: float, marge_m: float = 5.0) -> List[str]:
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
        return [n.strip() for n in re.findall(r"<lu:svnaam>([^<]*)</lu:svnaam>", xml)]

    def rup_region(self, x: float, y: float) -> List[str]:
        """Colonnes AH→BD."""
        return self._svnaam_categories("lu_gewrup_gv", x, y)

    def rup_province(self, x: float, y: float) -> List[str]:
        """Colonnes BG→CC."""
        return self._svnaam_categories("lu_prorup_gv", x, y)

    def rup_commune(self, x: float, y: float) -> List[str]:
        """Colonnes CF→DB."""
        return self._svnaam_categories("lu_gemrup_gv", x, y)
