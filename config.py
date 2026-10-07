"""Configuration centrale du pipeline Flandre (Belgique) — porté du socle
générique de `walon-map-france-cloud/config.py` (voir le plan
`je-voulais-dire-que-jazzy-hummingbird.md`), avec les URLs françaises
remplacées par les sources flamandes/belges confirmées en direct pendant
la recherche.

Contrairement à la France, pas de machinerie de registre de colonnes
dynamique (le gabarit vu n'a aucune icône ancrée) — voir
`services/colonnes_be.py` pour le mapping fixe lettre→rôle."""

from __future__ import annotations

from pathlib import Path

APP_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

TEMPLATE_PATH = BASE_DIR / "templates" / "gabarit_officiel.xlsx"
CACHE_DIR = BASE_DIR / "cache"
LOGS_DIR = BASE_DIR / "logs"
REGISTRY_DIR = BASE_DIR / "registry_data"
STATE_DIR = BASE_DIR / "state"

# Chantier RUP-seul (2026-10-07, voir le plan) : gabarit à une seule
# feuille ("Nom de RUP" + identité + 3 niveaux RUP + zones dynamiques),
# structure confirmée sur `Exemplaire.xlsx` (commune Aartselaar, exemple
# réel rempli par l'équipe) — totalement séparé du gabarit à 190 colonnes
# ci-dessus, jamais utilisé par main.py. Le cache PDF (rup_pdf_service.py)
# vit dans un sous-dossier de --cache-dir (voir main_rup.py), pas ici --
# doit respecter le même override que le reste du cache HTTP.
TEMPLATE_RUP_PATH = BASE_DIR / "templates" / "gabarit_rup.xlsx"

# Fichier de suivi (CSV, append-only) des cellules ayant échoué et à
# retenter — même motif que côté France (voir main.py::reessayer_cellules_*),
# PARTAGÉ entre toutes les communes traitées.
CELLULES_A_REVISITER_PATH = REGISTRY_DIR / "cellules_a_revisiter.csv"

# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------

MAX_REQUESTS_PER_SECOND = 5.0
HTTP_TIMEOUT_SECONDS = 30
HTTP_MAX_ATTEMPTS = 5
DEBUG = False  # surchargé par l'option --debug de main.py

# Vide par défaut tant qu'aucun service belge utilisé ne s'est montré
# instable de façon spécifique — même mécanisme que côté France
# (voir services/http_client.py::get_json).
SERVICE_TIMEOUT_SECONDS_OVERRIDES: dict[str, int] = {}
SERVICE_MAX_ATTEMPTS_OVERRIDES: dict[str, int] = {}

# ---------------------------------------------------------------------------
# APIs publiques flamandes/belges (vérifiées en direct, voir le plan)
# ---------------------------------------------------------------------------

# Basisregisters Vlaanderen — Adressenregister (liste d'adresses par
# commune+rue, lien OFFICIEL adresse->parcelle via /percelen). Confirmé en
# direct sur "Heilig-Hartplein"/Sint-Truiden (14 adresses, caPaKey cohérent).
ADRESSENREGISTER_BASE = "https://api.basisregisters.vlaanderen.be/v2"

# Cadastre fédéral belge (FOD Financiën / FPS Finance) — WFS 2.0 INSPIRE,
# feature type cp:CadastralParcel, CRS EPSG:4258. GetFeature EXIGE un BBOX
# (lat,lon) — confirmé en direct, pas de champ de total fiable
# (numberMatched="unknown"), contrairement à totalFeatures côté France.
CADASTRE_WFS_BASE = (
    "https://ccff02.minfin.fgov.be/geoservices/arcgis/rest/services/"
    "INSPIRE/CP/MapServer/exts/InspireFeatureDownload/service"
)

# Repli AUTOMATIQUE quand le service ci-dessus échoue -- incident réel
# 2026-10-06/07 : le serveur fédéral est tombé en panne plus de 24h
# d'affilée (java.lang.OutOfMemoryError côté serveur, confirmé via
# GetCapabilities). Service FLAMAND équivalent trouvé en vérifiant
# l'incident : "GRB - Administratieve percelen fiscaal" (Informatie
# Vlaanderen), géométrie des parcelles administratives qui CORRESPOND à
# celle des parcelles cadastrales gérées par l'AGDP/AAPD (même autorité
# que le WFS fédéral) -- infrastructure séparée, testé opérationnel
# pendant la panne fédérale, même convention caPaKey EXACTE (confirmé en
# direct : "31523F0581/00G000" retrouvé identique sur les deux services
# pour le même point). CRS natif EPSG:31370 (Lambert 72) -- pas de
# reprojection nécessaire pour la requête (contrairement au fédéral).
CADASTRE_ADPF_WFS_BASE = "https://geo.api.vlaanderen.be/Adpf/wfs"

# WFS Mercator (Departement Omgeving / Informatie Vlaanderen) — héberge
# Gewestplan (lu:lu_gwp_gv, VECTEUR — pas lu_gwp_rv_raster) et les 3
# niveaux de RUP (lu:lu_gewrup_gv région, lu:lu_provrup_gv province,
# lu:lu_gemrup_gv commune). Probablement EPSG:31370 (Lambert 72), comme le
# point d'adresse du registre — à confirmer par service.
MERCATOR_WFS_BASE = "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/ows"

# Watertoets / risque d'inondation — 2 hosts ArcGIS distincts confirmés en
# direct, EPSG:31370.
WATERTOETS_ADVIESKAART_WFS_BASE = "https://vha.waterinfo.be/arcgis/services/advieskaart_watertoets_WFS/MapServer/WFSServer"
WATERINFO_WFS_BASE = "http://inspirepub.waterinfo.be/arcgis/services/waterinfo_WFS/MapServer/WFSServer"

# CRS Lambert 72 (Belgique) — utilisé par la plupart des couches
# thématiques flamandes (Gewestplan/RUP/Watertoets), contre EPSG:4258 pour
# le cadastre fédéral. Nécessite une reprojection (pyproj) entre les deux
# quand on part d'un point cadastral pour interroger un WFS Lambert 72, ou
# inversement.
EPSG_LAMBERT72 = "EPSG:31370"
EPSG_CADASTRE = "EPSG:4258"
