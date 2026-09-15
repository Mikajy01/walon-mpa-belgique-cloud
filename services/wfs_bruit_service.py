"""Accès aux couches WFS de bruit (geluidsbelasting) — même host Mercator
(EPSG:3812) que Gewestplan/RUP. 3 couches confirmées en direct, noms
EXACTS vérifiés via le `<Title>` du `GetCapabilities` (jamais déduits du
nom de fichier seul — piège réel : `hh_weg_*` / `hh_weg_aan_*` /
`hh_agglo_weg_*` sont 3 couches DIFFÉRENTES pour "wegen", seul le titre
lève l'ambiguïté) :

- `hh:hh_weg_lnight_2021_c` = "belangrijke wegen Lnight" → colonnes FK→FO
- `hh:hh_lucht_lnight_2021_c` = "belangrijke luchthaven(s) Lnight" → FP→FT
- `hh:hh_spoor_lden_2021_c` = "belangrijke spoorwegen Lden" (PAS Lnight
  — vérifié sur le libellé réel de la colonne FU du gabarit, qui
  demande explicitement Lden pour ce thème, contrairement aux 2 autres) → FU→FY

Champ `category` confirmé en direct, ex. `"Lnight5054"` — encode
directement le palier de décibels (50-54 ≈ la tranche Excel "50-55 dB"),
mapping exact des bornes PAS encore calibré sur les 5 tranches (une
seule valeur observée)."""

from __future__ import annotations

import re
from typing import Optional

from services.http_client import HttpClient
from services.wfs_gewestplan_service import lambert72_vers_3812
from utils.logger import get_logger

_logger = get_logger("services.wfs_bruit_service")

_WFS_BASE = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ows"


class WfsBruitService:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @staticmethod
    def _bbox(x: float, y: float, marge_m: float = 5.0) -> str:
        return f"{x - marge_m},{y - marge_m},{x + marge_m},{y + marge_m},urn:ogc:def:crs:EPSG::3812"

    def _category(self, layer: str, x_l72: float, y_l72: float, marge_m: float = 5.0) -> Optional[str]:
        x, y = lambert72_vers_3812(x_l72, y_l72)
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"hh:{layer}", "count": 1,
            "BBOX": self._bbox(x, y, marge_m),
        }
        try:
            xml = self._http.get_text(_WFS_BASE, params, service_key="bruit_be")
        except Exception as exc:  # noqa: BLE001 — une couche indisponible ne doit jamais faire échouer tout le traitement de la parcelle
            _logger.warning("Couche '%s' indisponible (x=%s, y=%s) : %s", layer, x_l72, y_l72, exc)
            return None
        m = re.search(r"<hh:category>([^<]*)</hh:category>", xml)
        return m.group(1).strip() if m else None

    def bruit_routes(self, x: float, y: float) -> Optional[str]:
        """Colonnes FK→FO."""
        return self._category("hh_weg_lnight_2021_c", x, y)

    def bruit_aeroport(self, x: float, y: float) -> Optional[str]:
        """Colonnes FP→FT."""
        return self._category("hh_lucht_lnight_2021_c", x, y)

    def bruit_voies_ferrees(self, x: float, y: float) -> Optional[str]:
        """Colonnes FU→FY."""
        return self._category("hh_spoor_lden_2021_c", x, y)
