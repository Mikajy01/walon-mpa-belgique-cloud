"""Orchestrateur CLI — pipeline Flandre (Belgique). Voir le plan
`je-voulais-dire-que-jazzy-hummingbird.md` pour le contexte complet.

Contrairement à la France, pas de découverte "toute la commune" : une
liste de rues est TOUJOURS fournie explicitement (--rues), comme
demandé par l'utilisateur dès le départ pour ce besoin précis."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional

import config
from services.adressen_service import AdressenService
from services.cache_service import HttpCache
from services.cadastre_service import CadastreService
from services.decouverte_service import decouvrir_parcelles
from services.excel_service import (
    FIRST_DATA_ROW, charger_feuille, ecrire_ligne, lire_capakeys_deja_ecrits,
    sauvegarder, trouver_premiere_ligne_vide,
)
from services.http_client import HttpClient
from services.resolveur_service import ResolveurBE
from services.wfs_bodem_service import WfsBodemService
from services.wfs_bruit_service import WfsBruitService
from services.wfs_economie_service import WfsEconomieService
from services.wfs_advieskaart_service import WfsAdvieskaartService
from services.wfs_afstromingskaart_service import WfsAfstromingskaartService
from services.wfs_gewestplan_service import WfsGewestplanService
from services.wfs_landinrichting_woningbouw_service import (
    WfsLandinrichtingService, WfsNatuurinrichtingService, WfsWoningbouwService,
)
from services.wfs_landschap_service import WfsLandschapService
from services.wfs_natuur_service import WfsNatuurService
from services.wfs_grondverschuiving_service import WfsGrondverschuivingService
from services.wfs_grondwaterwinning_service import WfsGrondwaterwinningService
from services.wfs_ovam_service import WfsOvamService
from services.wfs_seveso_service import WfsSevesoService
from services.wfs_steunzone_brownfield_service import WfsSteunzoneBrownfieldService
from services.wfs_watertoets_service import WfsWatertoetsService
from utils.logger import get_logger, setup_logging
from utils.rate_limiter import RateLimiter

_logger = get_logger("main")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pipeline Flandre (Belgique).")
    parser.add_argument("--commune", required=True)
    parser.add_argument("--code-postal", required=True)
    parser.add_argument("--rues", required=True, help="Rues séparées par des virgules.")
    parser.add_argument("--template", default=str(config.TEMPLATE_PATH))
    parser.add_argument("--state-dir", default=str(config.STATE_DIR))
    parser.add_argument("--cache-dir", default=str(config.CACHE_DIR))
    parser.add_argument("--logs-dir", default=str(config.BASE_DIR / "logs"))
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def chemin_etat_commune(state_dir: Path, commune: str, code_postal: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", commune.strip().lower()).strip("-")
    return state_dir / f"{code_postal}_{slug}.xlsx"


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(args.logs_dir), debug=args.debug)

    cache = HttpCache(Path(args.cache_dir))
    rate_limiter = RateLimiter(config.MAX_REQUESTS_PER_SECOND)
    http = HttpClient(cache, rate_limiter, timeout=config.HTTP_TIMEOUT_SECONDS)

    adressen = AdressenService(http)
    cadastre = CadastreService(http)
    resolveur = ResolveurBE(
        WfsGewestplanService(http), WfsBruitService(http), WfsWatertoetsService(http),
        WfsEconomieService(http), WfsLandinrichtingService(http), WfsWoningbouwService(http),
        WfsNatuurinrichtingService(http), WfsLandschapService(http), WfsNatuurService(http),
        WfsBodemService(http), WfsSevesoService(http), WfsSteunzoneBrownfieldService(http),
        WfsOvamService(http), WfsGrondverschuivingService(http), WfsGrondwaterwinningService(http),
        WfsAfstromingskaartService(http), WfsAdvieskaartService(http),
    )

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    excel_path = chemin_etat_commune(state_dir, args.commune, args.code_postal)
    if not excel_path.exists():
        _logger.info("Aucun état existant pour '%s', amorçage depuis le gabarit (%s).", args.commune, excel_path)
        import shutil
        shutil.copyfile(Path(args.template), excel_path)

    rues = [r.strip() for r in re.split(r"[,\n]", args.rues) if r.strip()]

    n_total_ecrites = 0
    for rue in rues:
        ws = charger_feuille(excel_path)
        deja_ecrits = lire_capakeys_deja_ecrits(ws)
        _logger.info("Découverte de '%s' (%s)...", rue, args.commune)
        parcelles = decouvrir_parcelles(args.commune, rue, adressen, cadastre)
        _logger.info("'%s' : %d parcelle(s) trouvée(s), %d déjà écrite(s).", rue, len(parcelles), len(deja_ecrits))

        n_ecrites_rue = 0
        for p in parcelles:
            if p.parcelle.reference in deja_ecrits:
                continue
            a = p.adresses[0]
            valeurs = resolveur.resoudre(a.x, a.y)
            row = trouver_premiere_ligne_vide(ws)
            ecrire_ligne(
                ws, row, commune=args.commune, code_postal=args.code_postal, rue=rue,
                numero=a.huisnummer, capakey=p.parcelle.reference, valeurs=valeurs,
            )
            sauvegarder(ws, excel_path)
            ws = charger_feuille(excel_path)
            n_ecrites_rue += 1
            n_total_ecrites += 1

        _logger.info("'%s' : %d nouvelle(s) ligne(s) écrite(s).", rue, n_ecrites_rue)

    _logger.info("Résumé final : %d ligne(s) écrite(s) au total sur %d rue(s).", n_total_ecrites, len(rues))
    return 0


if __name__ == "__main__":
    sys.exit(main())
