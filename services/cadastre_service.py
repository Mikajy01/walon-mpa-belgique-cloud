"""Accès à la géométrie des parcelles cadastrales belges — WFS fédéral
INSPIRE (FOD Financiën / FPS Finance), `cp:CadastralParcel`.

PIÈGE RÉEL confirmé en direct en construisant ce module : `CQL_FILTER`
sur `label` OU sur `nationalCadastralReference` ne filtre PAS réellement
côté serveur — une requête `CQL_FILTER=nationalCadastralReference='X'`
renvoie des parcelles QUELCONQUES, sans rapport avec `X` (vérifié deux
fois, avec deux valeurs de filtre différentes, deux résultats
différents mais TOUJOURS faux). Ce WFS ArcGIS-hébergé ignore
silencieusement ce paramètre plutôt que de renvoyer une erreur ou un
résultat vide — donc **ne jamais faire confiance à un résultat CQL_FILTER
sans vérifier la référence réellement renvoyée**. Seule méthode fiable
trouvée : interroger par BBOX autour d'un point connu (comme
`services/wfs_georisques_service.py` côté France) puis chercher la
référence exacte attendue (`caPaKey`) parmi les résultats, en élargissant
le bbox si besoin — jamais accepter un résultat sans cette vérification
explicite.

Système de coordonnées : EPSG:4258 (~WGS84), ordre (latitude, longitude)
pour le BBOX — même convention que le WFS Géorisques français. Le point
de départ (position d'une adresse du registre) est lui en Lambert 72
(EPSG:31370) : reprojeté via `pyproj` avant toute requête ici."""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

import pyproj

import config
from models.parcelle import Parcelle
from services.http_client import HttpClient
from utils.logger import get_logger

_logger = get_logger("services.cadastre_service")

_RE_MEMBER = re.compile(
    r"<cp:nationalCadastralReference>([^<]*)</cp:nationalCadastralReference>.*?"
    r"<cp:areaValue[^>]*>([^<]*)</cp:areaValue>",
    re.S,
)
_RE_POSLIST = re.compile(r"<gml:posList>([^<]*)</gml:posList>")

# nis+section+numéro (ex. "44045D0247") -- base commune à plusieurs
# parcelles "sœurs" (bis-lettres différentes, ex. "247G"/"247S"/"247T")
# issues de la subdivision d'un même terrain d'origine -- voir
# `_base_parcelle`/`get_parcelle_et_voisines`.
_RE_REFERENCE = re.compile(r"^(\d{5}[A-Z]\d{4})/(\d{2})([A-Z_])(\d{3})$")

_TRANSFORMER_L72_VERS_4258 = pyproj.Transformer.from_crs("EPSG:31370", "EPSG:4258", always_xy=True)
_TRANSFORMER_4258_VERS_L72 = pyproj.Transformer.from_crs("EPSG:4258", "EPSG:31370", always_xy=True)

# Marges (m) d'élargissement progressif du bbox de recherche — la
# parcelle attendue est censée être TRÈS proche du point de départ (une
# adresse liée à elle par le registre), donc un bbox large n'est qu'un
# filet de sécurité, jamais le cas normal.
_MARGES_RECHERCHE_M = (100.0, 300.0, 1000.0)


def lambert72_vers_4258(x: float, y: float) -> tuple[float, float]:
    """Reprojette un point Lambert 72 (EPSG:31370, x/y) vers EPSG:4258
    (lon/lat) — utilisé pour interroger ce service à partir d'une
    position d'adresse du registre flamand (voir `adressen_service.py`)."""
    lon, lat = _TRANSFORMER_L72_VERS_4258.transform(x, y)
    return lon, lat


def vers_lambert72(lon: float, lat: float) -> tuple[float, float]:
    """Reprojection INVERSE de `lambert72_vers_4258` -- nécessaire pour
    positionner (Lambert 72, comme partout ailleurs dans le pipeline) une
    parcelle "sœur" SANS adresse propre à partir du centroïde de SA
    PROPRE géométrie (EPSG:4258, voir `_parser_geometrie`), voir
    `decouverte_service.py::_adresse_synthetique_sans_adresse`."""
    x, y = _TRANSFORMER_4258_VERS_L72.transform(lon, lat)
    return x, y


def _base_parcelle(reference: str) -> Optional[str]:
    """"44045D0247/00G000" -> "44045D0247" -- `None` si `reference` ne
    suit pas le motif attendu (jamais deviné, voir
    `excel_service.py::_RE_CAPAKEY_COMPLET`, même motif)."""
    m = _RE_REFERENCE.match(reference)
    return m.group(1) if m else None


class CadastreService:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @staticmethod
    def _bbox(lat: float, lon: float, marge_m: float, crs: str = "EPSG::4258") -> str:
        dlat = marge_m / 111320
        dlon = marge_m / (111320 * math.cos(math.radians(lat)))
        return f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon},urn:ogc:def:crs:{crs}"

    def get_parcelle(self, reference: str, lat: float, lon: float) -> Optional[Parcelle]:
        """Récupère la géométrie de la parcelle `reference` (caPaKey,
        ex. `"71053H1051/00N000"`), connue pour être proche de
        `(lat, lon)` (EPSG:4258) — jamais interrogée par CQL_FILTER (voir
        le docstring du module). Élargit le bbox progressivement
        (`_MARGES_RECHERCHE_M`) si la référence n'apparaît pas dans les
        premiers résultats ; renvoie `None` (jamais deviné) si elle reste
        introuvable après la marge la plus large."""
        parcelle, _voisines = self.get_parcelle_et_voisines(reference, lat, lon)
        return parcelle

    def get_parcelle_et_voisines(
        self, reference: str, lat: float, lon: float,
    ) -> Tuple[Optional[Parcelle], List[Parcelle]]:
        """Comme `get_parcelle`, mais renvoie EN PLUS les parcelles
        "sœurs" trouvées dans le MÊME bbox que `reference` (jamais un
        bbox séparé, uniquement des parcelles déjà rapportées par la
        requête qui a trouvé `reference`) : même référence de BASE
        (nis+section+numéro, voir `_base_parcelle`) mais un bis-lettre
        différent -- écart réel trouvé en investigation live (Mespelhoek
        7, Lokeren, 2026-09-19) : "247G" a une adresse officielle, mais
        "247S"/"247T" (visibles sur geopunt.be juste à côté) N'EN ONT
        AUCUNE dans le registre -- très probablement des subdivisions du
        même terrain (jardin/annexe). Décision explicite de l'utilisateur
        (2026-09-19) : ces parcelles SANS adresse doivent quand même être
        traitées (la checklist est par PARCELLE, pas par adresse) --
        voir `decouverte_service.py` pour la suite (adresse synthétique
        "/"). Liste vide si `reference` n'a aucune sœur dans ce bbox
        (cas normal, la plupart des parcelles n'ont pas de subdivision)."""
        for marge in _MARGES_RECHERCHE_M:
            parcelles = self._chercher_par_bbox(lat, lon, marge)
            cible = next((p for p in parcelles if p.reference == reference), None)
            if cible is not None:
                base = _base_parcelle(reference)
                voisines = (
                    [p for p in parcelles if p.reference != reference and _base_parcelle(p.reference) == base]
                    if base is not None else []
                )
                return cible, voisines
            _logger.debug(
                "Parcelle '%s' non trouvée dans un bbox de %.0fm autour de (%.6f, %.6f) "
                "(%d parcelle(s) vue(s)) — élargissement.", reference, marge, lat, lon, len(parcelles),
            )
        _logger.warning(
            "Parcelle '%s' introuvable près de (%.6f, %.6f) même après élargissement à %.0fm.",
            reference, lat, lon, _MARGES_RECHERCHE_M[-1],
        )
        return None, []

    def parcelles_autour(self, lat: float, lon: float, marge_m: float) -> List[Parcelle]:
        """Toutes les parcelles du bbox carré de demi-côté `marge_m` autour de
        `(lat, lon)` (EPSG:4258) -- utilisé par la découverte géométrique
        (`decouverte_geometrique_service.py`). ATTENTION : la requête est
        plafonnée à 200 entités (`count`), une réponse pleine peut donc être
        tronquée -- à l'appelant de le détecter (`len(...) >= 200`)."""
        return self._chercher_par_bbox(lat, lon, marge_m)

    def _chercher_par_bbox(self, lat: float, lon: float, marge_m: float) -> List[Parcelle]:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "cp:CadastralParcel", "count": 200,
            "BBOX": self._bbox(lat, lon, marge_m),
        }
        try:
            xml = self._http.get_text(config.CADASTRE_WFS_BASE, params, service_key="cadastre_be")
            return self._parser_membres(xml)
        except Exception as exc:  # noqa: BLE001 -- incident réel 2026-10-06/07 : le serveur fédéral
            # est tombé en panne (OutOfMemoryError côté serveur) plus de 24h d'affilée. Plutôt que de
            # faire échouer TOUTE la découverte en attendant un service dont on ne contrôle pas le
            # retour, repli automatique sur le service flamand équivalent (voir config.py,
            # CADASTRE_ADPF_WFS_BASE) -- mêmes caPaKey, infrastructure séparée. Jamais silencieux :
            # journalisé une seule fois par appel, pas par tentative (déjà fait par http_retry sur
            # l'appel fédéral lui-même).
            _logger.warning(
                "Cadastre fédéral indisponible (%s: %s) -- repli sur le service flamand équivalent "
                "(GRB Adpf).", type(exc).__name__, exc,
            )
            return self._chercher_par_bbox_adpf(lat, lon, marge_m)

    def _chercher_par_bbox_adpf(self, lat: float, lon: float, marge_m: float) -> List[Parcelle]:
        """Repli : même principe que `_chercher_par_bbox`, mais sur le WFS flamand
        `CADASTRE_ADPF_WFS_BASE` (voir config.py) -- structure de réponse différente
        (`Adpf:Adpf`/`Adpf:CAPAKEY`/`Adpf:SHAPE`, CRS natif EPSG:31370), voir
        `_parser_membres_adpf`."""
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "Adpf:Adpf", "count": 200,
            "BBOX": self._bbox(lat, lon, marge_m, crs="EPSG::4326"),
        }
        xml = self._http.get_text(config.CADASTRE_ADPF_WFS_BASE, params, service_key="cadastre_adpf_be")
        return self._parser_membres_adpf(xml)

    @staticmethod
    def _parser_membres(xml: str) -> List[Parcelle]:
        """Découpe la réponse GML en membres `cp:CadastralParcel` et
        extrait référence/aire/géométrie de chacun. Analyse texte simple
        (pas un vrai parseur XML) — même choix pragmatique que
        `wfs_georisques_service.py` côté France, la structure de ce WFS
        étant stable et connue."""
        parcelles: List[Parcelle] = []
        membres = xml.split("<wfs:member>")[1:]
        for bloc in membres:
            m_ref = re.search(r"<cp:nationalCadastralReference>([^<]*)</cp:nationalCadastralReference>", bloc)
            if not m_ref:
                continue
            reference = m_ref.group(1).strip()
            m_area = re.search(r"<cp:areaValue[^>]*>([^<]*)</cp:areaValue>", bloc)
            area = float(m_area.group(1)) if m_area else None
            geometry = CadastreService._parser_geometrie(bloc)
            parcelles.append(Parcelle(reference=reference, geometry=geometry, area=area))
        return parcelles

    @staticmethod
    def _parser_geometrie(bloc: str) -> Optional[Dict]:
        """Convertit le(s) `gml:posList` (ordre lat,lon, EPSG:4258) en
        géométrie GeoJSON (ordre lon,lat) — `utils/geometrie.py` attend
        cet ordre (voir `centroide_geometrie`/`point_dans_geometrie`).
        Ne gère qu'un seul anneau extérieur par polygone : aucun trou
        constaté sur les parcelles vues en investigation live (même
        hypothèse que côté France)."""
        anneaux = _RE_POSLIST.findall(bloc)
        if not anneaux:
            return None
        polygones = []
        for anneau_texte in anneaux:
            valeurs = [float(v) for v in anneau_texte.split()]
            paires = list(zip(valeurs[0::2], valeurs[1::2]))  # (lat, lon)
            anneau = [[lon, lat] for lat, lon in paires]
            polygones.append([anneau])
        if len(polygones) == 1:
            return {"type": "Polygon", "coordinates": polygones[0]}
        return {"type": "MultiPolygon", "coordinates": polygones}

    @staticmethod
    def _parser_membres_adpf(xml: str) -> List[Parcelle]:
        """Comme `_parser_membres`, pour le format du WFS de repli (voir
        config.py, `CADASTRE_ADPF_WFS_BASE`) : membres `<Adpf:Adpf>`,
        référence dans `<Adpf:CAPAKEY>` (même convention caPaKey que le
        fédéral, vérifié en direct), pas de champ d'aire exploité (`area`
        n'est utilisé nulle part dans le reste du pipeline -- laissé à
        `None`)."""
        parcelles: List[Parcelle] = []
        membres = xml.split("<wfs:member>")[1:]
        for bloc in membres:
            m_ref = re.search(r"<Adpf:CAPAKEY>([^<]*)</Adpf:CAPAKEY>", bloc)
            if not m_ref:
                continue
            reference = m_ref.group(1).strip()
            geometry = CadastreService._parser_geometrie_adpf(bloc)
            parcelles.append(Parcelle(reference=reference, geometry=geometry, area=None))
        return parcelles

    @staticmethod
    def _parser_geometrie_adpf(bloc: str) -> Optional[Dict]:
        """Convertit le(s) `gml:posList` du `<Adpf:SHAPE>` -- CRS NATIF
        EPSG:31370 (Lambert 72, ordre x,y -- confirmé en direct, valeurs
        dans la plage attendue pour la Belgique), PAS EPSG:4258 comme le
        fédéral -- reprojeté point par point vers EPSG:4258 (lon,lat) via
        `lambert72_vers_4258`, pour que `Parcelle.geometry` garde EXACTEMENT
        la même convention quelle que soit la source ayant répondu, et que
        tout le reste du pipeline (déjà écrit pour le fédéral) n'ait rien à
        changer. Même hypothèse qu'ailleurs : un seul anneau extérieur par
        polygone, pas de trou géré."""
        anneaux = _RE_POSLIST.findall(bloc)
        if not anneaux:
            return None
        polygones = []
        for anneau_texte in anneaux:
            valeurs = [float(v) for v in anneau_texte.split()]
            paires_l72 = list(zip(valeurs[0::2], valeurs[1::2]))  # (x, y) Lambert 72
            anneau = [list(lambert72_vers_4258(x, y)) for x, y in paires_l72]  # [lon, lat]
            polygones.append([anneau])
        if len(polygones) == 1:
            return {"type": "Polygon", "coordinates": polygones[0]}
        return {"type": "MultiPolygon", "coordinates": polygones}
