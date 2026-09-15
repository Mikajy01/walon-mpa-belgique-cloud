"""Accès aux couches WFS "nature" confirmées en direct sur le host
Mercator (EPSG:3812) — existence seule (motif `_existe`) :

- `ps:ps_ven` (VEN — Vlaams Ecologisch Netwerk) → colonne FH. Champ
  `categorie` disponible (ex. "geno") mais pas encore nécessaire, la
  colonne Excel FH ne distingue pas de sous-catégorie.
- `ps:ps_duin` (gebieden duinendecreet) → colonne FG.

Colonnes FD (Natura 2000 Habitatkaart/Beheergebieden), FE
(Bosreservaten/Erkende Natuurreservaten — 2 couches probablement
distinctes), FI (Habitatrichtlijngebieden), FJ (Vogelrichtlijngebieden)
— PAS encore de couche confirmée sur ce host (recherche par mot-clé
"habitat"/"vogel"/"natuurreservaat" infructueuse dans le
GetCapabilities complet) : probablement un host séparé (Natura 2000 est
souvent une source INSPIRE distincte), à investiguer."""

from __future__ import annotations

import re
from typing import Optional

from services.http_client import HttpClient
from services.wfs_gewestplan_service import lambert72_vers_3812
from utils.logger import get_logger

_logger = get_logger("services.wfs_natuur_service")

_WFS_BASE = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ows"


class WfsNatuurService:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @staticmethod
    def _bbox(x: float, y: float, marge_m: float = 5.0) -> str:
        return f"{x - marge_m},{y - marge_m},{x + marge_m},{y + marge_m},urn:ogc:def:crs:EPSG::3812"

    def _existe(self, namespace: str, layer: str, x_l72: float, y_l72: float, marge_m: float = 5.0) -> Optional[str]:
        x, y = lambert72_vers_3812(x_l72, y_l72)
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"{namespace}:{layer}", "count": 1,
            "BBOX": self._bbox(x, y, marge_m),
        }
        try:
            xml = self._http.get_text(_WFS_BASE, params, service_key="natuur_be")
        except Exception as exc:  # noqa: BLE001 — une couche indisponible ne doit jamais faire échouer tout le traitement de la parcelle
            _logger.warning("Couche '%s:%s' indisponible (x=%s, y=%s) : %s", namespace, layer, x_l72, y_l72, exc)
            return None
        m = re.search(r'numberReturned="(\d+)"', xml)
        if not m:
            return None
        return "O" if int(m.group(1)) > 0 else "N"

    def ven(self, x: float, y: float) -> Optional[str]:
        """Colonne FH."""
        return self._existe("ps", "ps_ven", x, y)

    def duinendecreet(self, x: float, y: float) -> Optional[str]:
        """Colonne FG."""
        return self._existe("ps", "ps_duin", x, y)
