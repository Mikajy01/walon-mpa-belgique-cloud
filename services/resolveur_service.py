"""Orchestration : résout TOUTES les colonnes confirmées (84/184, voir
le plan) pour un point donné (position d'adresse, Lambert 72) en
appelant chaque service thématique déjà construit et vérifié.

Principe de sortie : `Dict[str, str]` lettre de colonne Excel -> valeur
("O"/"N"/texte). Seules les valeurs RÉELLEMENT confirmées sont incluses
— pour les groupes "un seul parmi plusieurs" (Gewestplan, paliers
watertoets, bruit), seule la colonne gagnante est écrite ("O"), les
colonnes sœurs restent ABSENTES du dict (donc la cellule Excel reste
vide) plutôt que d'écrire "N" sans certitude sur les autres paliers
possibles — décision délibérée, jamais de "N" deviné sur un groupe à
choix unique tant que le remplissage systématique (une seule couche
peut suffire à confirmer TOUTES les autres colonnes du groupe comme
"N") n'est pas construit et vérifié."""

from __future__ import annotations

from typing import Dict, Optional

from services.wfs_advieskaart_service import WfsAdvieskaartService
from services.wfs_afstromingskaart_service import WfsAfstromingskaartService
from services.wfs_gewestplan_service import WfsGewestplanService
from services.wfs_bruit_service import WfsBruitService
from services.wfs_watertoets_service import WfsWatertoetsService
from services.wfs_economie_service import WfsEconomieService
from services.wfs_landinrichting_woningbouw_service import (
    WfsLandinrichtingService, WfsWoningbouwService, WfsNatuurinrichtingService,
)
from services.wfs_landschap_service import WfsLandschapService
from services.wfs_natuur_service import WfsNatuurService
from services.wfs_bodem_service import WfsBodemService
from services.wfs_grondverschuiving_service import WfsGrondverschuivingService
from services.wfs_grondwaterwinning_service import WfsGrondwaterwinningService
from services.wfs_ovam_service import WfsOvamService
from services.wfs_seveso_service import WfsSevesoService
from services.wfs_steunzone_brownfield_service import WfsSteunzoneBrownfieldService
from models.colonnes_be import COLONNES_BE
from utils.text_normalize import meilleure_correspondance
from utils.logger import get_logger

_logger = get_logger("services.resolveur_service")


class ResolveurBE:
    def __init__(
        self, gewestplan: WfsGewestplanService, bruit: WfsBruitService,
        watertoets: WfsWatertoetsService, economie: WfsEconomieService,
        landinrichting: WfsLandinrichtingService, woningbouw: WfsWoningbouwService,
        natuurinrichting: WfsNatuurinrichtingService, landschap: WfsLandschapService,
        natuur: WfsNatuurService, bodem: WfsBodemService, seveso: WfsSevesoService,
        steunzone_brownfield: WfsSteunzoneBrownfieldService, ovam: WfsOvamService,
        grondverschuiving: WfsGrondverschuivingService,
        grondwaterwinning: WfsGrondwaterwinningService,
        afstromingskaart: WfsAfstromingskaartService, advieskaart: WfsAdvieskaartService,
    ) -> None:
        self._gewestplan = gewestplan
        self._bruit = bruit
        self._watertoets = watertoets
        self._economie = economie
        self._landinrichting = landinrichting
        self._woningbouw = woningbouw
        self._natuurinrichting = natuurinrichting
        self._landschap = landschap
        self._natuur = natuur
        self._bodem = bodem
        self._seveso = seveso
        self._steunzone_brownfield = steunzone_brownfield
        self._ovam = ovam
        self._grondverschuiving = grondverschuiving
        self._grondwaterwinning = grondwaterwinning
        self._afstromingskaart = afstromingskaart
        self._advieskaart = advieskaart

    def resoudre(self, x: float, y: float) -> Dict[str, str]:
        """`x`/`y` : position (Lambert 72, EPSG:31370) — la position de
        l'adresse liée à la parcelle (voir `AdresseBE`)."""
        valeurs: Dict[str, str] = {}

        # -- Gewestplan (un seul parmi 26, correspondance floue) -------
        candidats_gwp = {l: i["categorie"] for l, i in COLONNES_BE.items() if i["groupe"] == "gewestplan"}
        for svnaam in self._gewestplan.svnaam_categories(x, y):
            colonne = meilleure_correspondance(svnaam, candidats_gwp)
            if colonne:
                valeurs[colonne] = "O"
            else:
                _logger.warning("Gewestplan : svnaam '%s' sans correspondance >= seuil.", svnaam)

        # -- Bruit (un seul parmi 5, par thème) -------------------------
        for methode in (
            self._bruit.colonne_bruit_routes, self._bruit.colonne_bruit_aeroport,
            self._bruit.colonne_bruit_voies_ferrees,
        ):
            colonne = methode(x, y)
            if colonne:
                valeurs[colonne] = "O"

        # -- Watertoets ---------------------------------------------------
        overstr = self._watertoets.overstromingsgevoelig(x, y)
        if overstr == "mogelijk":
            valeurs["DC"] = "O"
        elif overstr == "effectief":
            valeurs["DD"] = "O"

        signaal = self._watertoets.signaalgebied_categorie(x, y)
        if signaal == "Bouwvrije opgave":
            valeurs["DT"] = "O"
        elif signaal == "Verscherpte watertoets":
            valeurs["DU"] = "O"

        if self._watertoets.risicozone(x, y):
            valeurs["DX"] = "O"

        lbl = self._watertoets.van_nature_overstroombaar(x, y)
        if lbl:
            valeurs["DW"] = "O" if "niet" not in lbl.lower() else "N"

        if self._watertoets.overstromingsgebied_oeverzone_iwb(x, y) == "O":
            valeurs["DZ"] = "O"

        iwb = self._watertoets.colonne_afgebakend_oeverzone_iwb(x, y)
        if iwb:
            valeurs[iwb] = "O"

        for methode in (
            self._watertoets.colonne_overstromingsgevoelig_pluviaal,
            self._watertoets.colonne_overstromingsgevoelig_fluviaal,
            self._watertoets.colonne_overstromingsgevoelig_zee,
        ):
            colonne = methode(x, y)
            if colonne:
                valeurs[colonne] = "O"

        # -- Économie (EN valeur brute + EO->ER existence) ---------------
        aangeboden = self._economie.aangeboden_perceel(x, y)
        if aangeboden is not None:
            valeurs["EN"] = "O" if aangeboden == "aangeboden" else "N"
        for lettre, methode in (
            ("EO", self._economie.bedrijventerrein), ("EP", self._economie.beheerde_bedrijvenzone),
            ("EQ", self._economie.ontwikkelbare_bedrijvenzone), ("ER", self._economie.planningszone),
        ):
            v = methode(x, y)
            if v is not None:
                valeurs[lettre] = v

        # -- Landinrichting / Natuurinrichting / Woningbouw --------------
        for lettre, methode in (
            ("EW", self._landinrichting.landinrichting_in_onderzoek),
            ("EX", self._landinrichting.vastgesteld_landinrichtingsproject),
            ("EY", self._landinrichting.landinrichtingsplan),
            ("EZ", self._natuurinrichting.natuurinrichting),
            ("GF", self._woningbouw.woningbouwgebied),
            ("GG", self._woningbouw.woonvernieuwingsgebied),
        ):
            v = methode(x, y)
            if v is not None:
                valeurs[lettre] = v

        # -- Landschap ------------------------------------------------
        fb = self._landschap.fysische_systeemeenheid(x, y)
        if fb is not None:
            valeurs["FB"] = "N" if fb == "Onbepaald" else "O"
        fc = self._landschap.traditioneel_landschap(x, y)
        if fc is not None:
            valeurs["FC"] = fc

        # -- Nature -----------------------------------------------------
        for lettre, methode in (
            ("FG", self._natuur.duinendecreet), ("FH", self._natuur.ven),
            ("FI", self._natuur.habitatrichtlijngebied), ("FJ", self._natuur.vogelrichtlijngebied),
            ("FF", self._natuur.erkend_natuurreservaat),
        ):
            v = methode(x, y)
            if v is not None:
                valeurs[lettre] = v

        # -- Bodem (érosion) ---------------------------------------------
        for lettre, methode in (
            ("EL", self._bodem.potentiele_bodemerosie), ("EI", self._bodem.andere_erosiegerelateerde_gronden),
        ):
            v = methode(x, y)
            if v is not None:
                valeurs[lettre] = v

        # -- Seveso -------------------------------------------------------
        for lettre, methode in (
            ("EU", self._seveso.seveso_inrichting), ("EV", self._seveso.seveso_consultatiezone),
        ):
            v = methode(x, y)
            if v is not None:
                valeurs[lettre] = v

        # -- Steunzone / Brownfield ---------------------------------------
        for lettre, methode in (
            ("ET", self._steunzone_brownfield.steunzone),
            ("ES", self._steunzone_brownfield.brownfieldconvenant),
        ):
            v = methode(x, y)
            if v is not None:
                valeurs[lettre] = v

        # -- OVAM (pollution des sols) -------------------------------
        colonne_ovam = self._ovam.colonne_bodemverontreiniging(x, y)
        if colonne_ovam:
            valeurs[colonne_ovam] = "O"

        # -- Grondverschuivingen -----------------------------------------
        for lettre, methode in (
            ("EJ", self._grondverschuiving.gekarteerde_grondverschuiving),
            ("EK", self._grondverschuiving.gevoeligheid_grondverschuiving),
        ):
            v = methode(x, y)
            if v is not None:
                valeurs[lettre] = v

        v = self._grondwaterwinning.grondwaterwingebied(x, y)
        if v is not None:
            valeurs["EA"] = v

        colonne_afstr = self._afstromingskaart.colonne_afstromingskaart(x, y)
        if colonne_afstr:
            valeurs[colonne_afstr] = "O"

        v = self._advieskaart.watertoets(x, y)
        if v is not None:
            valeurs["EM"] = v

        # -- RUP (AF->DB, 75 colonnes) -----------------------------------
        # Décision explicite de l'utilisateur (2026-09-16) : le bloc RUP
        # détaillé de la feuille principale n'est PAS rempli par le
        # pipeline (classement fiable non construit, voir le plan) --
        # écrit "/" partout, même convention que le gabarit officiel pour
        # une cellule non applicable (voir ligne 4 des en-têtes). Le
        # vrai remplissage RUP se fait à la main sur la feuille "RUP"
        # dédiée (lien + texte libre par niveau), pas ici.
        for lettre, info in COLONNES_BE.items():
            if info["groupe"] in ("rup_region", "rup_province", "rup_commune"):
                valeurs[lettre] = "/"

        return valeurs
