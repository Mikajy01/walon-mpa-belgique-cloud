"""Accès aux couches WFS/WMS "waterinfo.be" (risque d'inondation
flamand) — voir le plan pour le détail des colonnes DC→EB couvertes.

**Couches WFS** (`inspirepub.waterinfo.be/.../waterinfo_WFS`) :
`Overstromingsgevoelige_gebieden_2017` (champ `overstrgev` :
"mogelijk"/"effectief" → DC/DD), `Goedgekeurde_signaalgebieden` (champ
`Categorie` : "Bouwvrije opgave"/"Verscherpte watertoets" → DT/DU),
`Risicozones_2017` (champ `risicozone` → DX).

**Couches WMS, accès via `GetFeatureInfo`** (`inspirepub.waterinfo.be/
.../informatieplicht/<nom>/MapServer/WMSServer`) — PAS de WFS activé sur
ces MapServer précis (`WFSServer` renvoie une erreur ArcGIS générique
HTTP 400, confirmé en direct), mais `GetFeatureInfo` fonctionne et
renvoie un XML `<FIELDS>` exploitable par une simple regex, même esprit
que le parsing GML du reste du projet :
`overstromingsgevoelige_gebieden_pluviaal`/`_fluviaal`/`_vanuit_de_zee`
— champ `gridcode`, confirmé en direct avec une vraie valeur (`1`) sur
un point réel → colonnes DF→DI (Pluvial), DJ→DM (Fluvial), DN→DQ
(vanuit zee). Mapping gridcode -> palier de probabilité PAS encore
confirmé (une seule valeur observée) — à affiner avec plusieurs points
réels de paliers différents avant mise en production.

**Couches WFS supplémentaires** (`geo.api.vlaanderen.be`, trouvées via
le catalogue officiel `metadata.vlaanderen.be`, PAS `mercator.vlaanderen.be`) :
`NOG/wfs` (`NOG:Nog`, champ `LBLNATOORZ` → colonne DW),
`OGOZ/wfs` (`OGOZ:Ogoz`, existence seule → colonne DZ).

Colonnes DE, DR/DS, DV, DY, EA, EB — PAS encore de source confirmée
(voir le plan)."""

from __future__ import annotations

import re
from typing import Optional

from services.http_client import HttpClient
from utils.logger import get_logger

_logger = get_logger("services.wfs_watertoets_service")

_WFS_BASE = "http://inspirepub.waterinfo.be/arcgis/services/waterinfo_WFS/MapServer/WFSServer"
_WMS_INFORMATIEPLICHT_BASE = "https://inspirepub.waterinfo.be/arcgis/services/informatieplicht/{dataset}/MapServer/WMSServer"
_NOG_WFS_BASE = "https://geo.api.vlaanderen.be/NOG/wfs"
_OGOZ_WFS_BASE = "https://geo.api.vlaanderen.be/OGOZ/wfs"


class WfsWatertoetsService:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    # -- bbox Lambert 72 (mètres, pas de conversion degrés) -------------

    @staticmethod
    def _bbox(x: float, y: float, marge_m: float = 5.0) -> str:
        return f"{x - marge_m},{y - marge_m},{x + marge_m},{y + marge_m},urn:ogc:def:crs:EPSG::31370"

    # -- couches WFS classiques ------------------------------------------

    def _valeur_champ_wfs(self, base_url: str, namespace: str, typename: str, champ: str, x: float, y: float) -> Optional[str]:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"{namespace}:{typename}", "count": 1,
            "BBOX": self._bbox(x, y),
        }
        try:
            xml = self._http.get_text(base_url, params, service_key="watertoets")
        except Exception as exc:  # noqa: BLE001 — une couche indisponible ne doit jamais faire échouer tout le traitement de la parcelle
            _logger.warning("Couche '%s:%s' indisponible (x=%s, y=%s) : %s", namespace, typename, x, y, exc)
            return None
        m = re.search(rf"<{namespace}:{champ}>([^<]*)</{namespace}:{champ}>", xml)
        return m.group(1).strip() if m else None

    def _existe_wfs(self, base_url: str, namespace: str, typename: str, x: float, y: float) -> Optional[str]:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"{namespace}:{typename}", "count": 1,
            "BBOX": self._bbox(x, y),
        }
        try:
            xml = self._http.get_text(base_url, params, service_key="watertoets")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Couche '%s:%s' indisponible (x=%s, y=%s) : %s", namespace, typename, x, y, exc)
            return None
        m = re.search(r'numberReturned="(\d+)"', xml)
        if not m:
            return None
        return "O" if int(m.group(1)) > 0 else "N"

    def overstromingsgevoelig(self, x: float, y: float) -> Optional[str]:
        """"mogelijk"/"effectief"/`None` — colonnes DC/DD."""
        return self._valeur_champ_wfs(_WFS_BASE, "waterinfo_WFS", "Overstromingsgevoelige_gebieden_2017", "overstrgev", x, y)

    def signaalgebied_categorie(self, x: float, y: float) -> Optional[str]:
        """"Bouwvrije opgave"/"Verscherpte watertoets"/`None` — colonnes DT/DU."""
        return self._valeur_champ_wfs(_WFS_BASE, "waterinfo_WFS", "Goedgekeurde_signaalgebieden", "Categorie", x, y)

    def risicozone(self, x: float, y: float) -> Optional[str]:
        """Colonne DX."""
        return self._valeur_champ_wfs(_WFS_BASE, "waterinfo_WFS", "Risicozones_2017", "risicozone", x, y)

    def van_nature_overstroombaar(self, x: float, y: float) -> Optional[str]:
        """Champ `LBLNATOORZ` — colonne DW."""
        return self._valeur_champ_wfs(_NOG_WFS_BASE, "NOG", "Nog", "LBLNATOORZ", x, y)

    def overstromingsgebied_oeverzone_iwb(self, x: float, y: float) -> Optional[str]:
        """Existence seule — colonne DZ."""
        return self._existe_wfs(_OGOZ_WFS_BASE, "OGOZ", "Ogoz", x, y)

    # -- couches WMS (GetFeatureInfo, pas de WFS sur ces MapServer) -----

    def _gridcode_wms(self, dataset: str, x: float, y: float, marge_m: float = 5.0) -> Optional[str]:
        """`GetFeatureInfo` sur le MapServer "informatieplicht/<dataset>",
        renvoie le `gridcode` brut du premier `<FIELDS>` trouvé (mapping
        exact code->palier pas encore confirmé, voir le docstring du
        module) — `None` si aucune donnée à ce point."""
        url = _WMS_INFORMATIEPLICHT_BASE.format(dataset=dataset)
        params = {
            "service": "WMS", "version": "1.3.0", "request": "GetFeatureInfo",
            "layers": "0", "query_layers": "0", "crs": "EPSG:31370",
            "bbox": f"{x - marge_m},{y - marge_m},{x + marge_m},{y + marge_m}",
            "width": "101", "height": "101", "i": "50", "j": "50",
            "info_format": "text/xml",
        }
        try:
            xml = self._http.get_text(url, params, service_key="watertoets_wms")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Couche WMS '%s' indisponible (x=%s, y=%s) : %s", dataset, x, y, exc)
            return None
        m = re.search(r'gridcode="([^"]*)"', xml)
        return m.group(1) if m else None

    def overstromingsgevoelig_pluviaal_gridcode(self, x: float, y: float) -> Optional[str]:
        """Colonnes DF→DI (palier de probabilité pluvial)."""
        return self._gridcode_wms("overstromingsgevoelige_gebieden_pluviaal", x, y)

    def overstromingsgevoelig_fluviaal_gridcode(self, x: float, y: float) -> Optional[str]:
        """Colonnes DJ→DM (palier de probabilité fluvial)."""
        return self._gridcode_wms("overstromingsgevoelige_gebieden_fluviaal", x, y)

    def overstromingsgevoelig_zee_gridcode(self, x: float, y: float) -> Optional[str]:
        """Colonnes DN→DQ (palier de probabilité depuis la mer)."""
        return self._gridcode_wms("overstromingsgevoelige_gebieden_vanuit_de_zee", x, y)
