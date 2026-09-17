"""Orchestrateur CLI — pipeline Flandre (Belgique). Voir le plan
`je-voulais-dire-que-jazzy-hummingbird.md` pour le contexte complet.

Contrairement à la France, pas de découverte "toute la commune" : une
liste de rues est TOUJOURS fournie explicitement (--rues), comme
demandé par l'utilisateur dès le départ pour ce besoin précis."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import config
from services.adressen_service import AdressenService
from services.cache_service import HttpCache
from services.cadastre_service import CadastreService
from services.decouverte_service import decouvrir_parcelles
from services.excel_service import (
    charger_classeur, ecrire_identite, ecrire_ligne, ecrire_rup,
    ecrire_zones_dynamiques_rup, feuille_principale, feuille_rup, index_colonnes_dynamiques_rup,
    lire_capakeys_deja_ecrits, lire_capakeys_vers_lignes, sauvegarder, trouver_ou_creer_colonne_dynamique_rup,
    trouver_premiere_ligne_vide, vers_capakey_court,
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
    WfsLandinrichtingService, WfsNatuurinrichtingService, WfsWoningbouwService, WfsRuilverkavelingService,
)
from services.wfs_landschap_service import WfsLandschapService
from services.wfs_natuur_service import WfsNatuurService
from services.wfs_grondverschuiving_service import WfsGrondverschuivingService
from services.wfs_grondwaterwinning_service import WfsGrondwaterwinningService
from services.wfs_ovam_service import WfsOvamService
from services.wfs_rup_service import WfsRupService
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
    parser.add_argument(
        "--budget-heures", type=float, default=5.5,
        help=(
            "Budget de temps interne (heures) -- au-delà, le run s'arrête "
            "PROPREMENT (sauvegarde déjà faite à chaque parcelle) avant "
            "qu'un timeout externe (ex: GitHub Actions) ne tue le job de "
            "force, ce qui sauterait l'étape de commit. Incident réel "
            "confirmé le 2026-09-16 : un run de 712 adresses annulé après "
            "3h par le timeout du workflow, aucune sauvegarde poussée "
            "(voir traiter_commune.yml). Défaut (5h30) choisi pour garder "
            "30 min de marge sous le timeout-minutes du workflow (6h, le "
            "plafond dur GitHub) -- ne JAMAIS remonter l'un sans "
            "revérifier l'autre."
        ),
    )
    return parser.parse_args(argv)


def chemin_etat_commune(state_dir: Path, commune: str, code_postal: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", commune.strip().lower()).strip("-")
    return state_dir / f"{code_postal}_{slug}.xlsx"


def _log_progres(prefixe: str, actuel: int, total: int, largeur: int = 30) -> None:
    """Barre de progression textuelle dans les logs -- demande du
    2026-09-17. Affichée seulement à des paliers d'environ 5% (+ le
    tout premier et le tout dernier élément) pour ne pas noyer les logs
    d'une ligne par parcelle sur une rue de 400 adresses."""
    if total <= 0:
        return
    palier = max(1, total // 20)
    if actuel != 1 and actuel != total and actuel % palier != 0:
        return
    pct = actuel / total
    n_rempli = int(largeur * pct)
    barre = "#" * n_rempli + "-" * (largeur - n_rempli)
    _logger.info("%s [%s] %3d%% (%d/%d)", prefixe, barre, int(pct * 100), actuel, total)


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
        WfsAfstromingskaartService(http), WfsAdvieskaartService(http), WfsRuilverkavelingService(http),
    )
    rup = WfsRupService(http)

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    excel_path = chemin_etat_commune(state_dir, args.commune, args.code_postal)
    if not excel_path.exists():
        _logger.info("Aucun état existant pour '%s', amorçage depuis le gabarit (%s).", args.commune, excel_path)
        import shutil
        shutil.copyfile(Path(args.template), excel_path)

    rues = [r.strip() for r in re.split(r"[,\n]", args.rues) if r.strip()]

    deadline = datetime.now(timezone.utc) + timedelta(hours=args.budget_heures)
    incomplet = False

    # Accumulé sur TOUTES les rues de ce run (neuves ET déjà écrites) --
    # nécessaire pour la réconciliation finale des colonnes RUP
    # dynamiques (voir plus bas et excel_service.py) : une colonne
    # découverte en traitant la 8e rue doit aussi être remplie pour les
    # lignes des 7 rues précédentes de CE MÊME run, pas seulement les
    # lignes écrites après sa création.
    suivi_reconciliation_rup: List[tuple] = []

    n_total_ecrites = 0
    for rue in rues:
        if datetime.now(timezone.utc) >= deadline:
            _logger.warning(
                "Budget de temps (%.1fh) atteint avant '%s' -- rue (et toutes les suivantes) reportée(s) "
                "au prochain run, rien n'est perdu (les rues déjà traitées restent sauvegardées).",
                args.budget_heures, rue,
            )
            incomplet = True
            break

        wb = charger_classeur(excel_path)
        ws = feuille_principale(wb)
        ws_rup = feuille_rup(wb)
        deja_ecrits = lire_capakeys_deja_ecrits(ws)
        capakeys_vers_lignes_rup = lire_capakeys_vers_lignes(ws_rup) if ws_rup is not None else {}
        _logger.info("Découverte de '%s' (%s)...", rue, args.commune)
        parcelles = decouvrir_parcelles(args.commune, rue, adressen, cadastre)
        _logger.info("'%s' : %d parcelle(s) trouvée(s), %d déjà écrite(s).", rue, len(parcelles), len(deja_ecrits))

        n_ecrites_rue = 0
        for i_parcelle, p in enumerate(parcelles):
            _log_progres(f"'{rue}'", i_parcelle + 1, len(parcelles))
            # Format court affiché sur geopunt.be (ex. "1081", "1049B"),
            # PAS le CaPaKey complet du WFS fédéral -- demande explicite
            # de l'utilisateur du 2026-09-17, appliquée une seule fois
            # ici puis réutilisée pour la comparaison "déjà écrit" ET
            # les deux feuilles (principale + RUP), pour que les deux
            # restent cohérentes entre elles et avec ce qui est déjà
            # sur disque (voir excel_service.py::vers_capakey_court).
            capakey_court = vers_capakey_court(p.parcelle.reference)
            if capakey_court in deja_ecrits:
                # Déjà écrite (run précédent) : toujours suivie pour la
                # réconciliation RUP dynamique plus bas (une nouvelle
                # colonne découverte plus tard dans CE run doit aussi
                # être remplie pour cette ligne), même si rien d'autre
                # n'est retraité ici.
                if ws_rup is not None:
                    ligne_rup = capakeys_vers_lignes_rup.get(capakey_court)
                    if ligne_rup is not None:
                        a = p.adresses[0]
                        suivi_reconciliation_rup.append((ligne_rup, a.x, a.y))
                continue
            if datetime.now(timezone.utc) >= deadline:
                _logger.warning(
                    "Budget de temps (%.1fh) atteint EN COURS de '%s' -- %d/%d parcelle(s) de cette rue "
                    "traitée(s), le reste (et les rues suivantes) reporté au prochain run.",
                    args.budget_heures, rue, n_ecrites_rue, len(parcelles),
                )
                incomplet = True
                break
            a = p.adresses[0]
            valeurs = resolveur.resoudre(a.x, a.y)
            row = trouver_premiere_ligne_vide(ws)
            ecrire_ligne(
                ws, row, commune=args.commune, code_postal=args.code_postal, rue=rue,
                numero=a.huisnummer, capakey=capakey_court, valeurs=valeurs,
            )
            if ws_rup is not None:
                # Même ligne que la feuille principale. Décision du
                # 2026-09-16 : pas de classement dans les ~25 catégories
                # détaillées (jamais construit, risque de classement
                # faux -- voir le plan), mais on RAPPORTE directement ce
                # que l'API donne déjà pour ce point précis (lien vers
                # la fiche + code officiel du plan), sans aucune
                # supposition. Colonne "RUP" = `algplanid` (ex.
                # "RUP_71053_214_00026_00026"), PAS le nom lisible
                # naam/svnaam -- correction du 2026-09-17 après
                # comparaison avec un fichier traité manuellement.
                ecrire_identite(
                    ws_rup, row, commune=args.commune, code_postal=args.code_postal, rue=rue,
                    numero=a.huisnummer, capakey=capakey_court,
                )
                for niveau, methode in (
                    ("region", rup.rup_region), ("province", rup.rup_province), ("commune", rup.rup_commune),
                ):
                    infos = methode(a.x, a.y)
                    if not infos:
                        # Aucun RUP de ce niveau à ce point -- "/" plutôt
                        # que vide (demande du 2026-09-17 : jamais de
                        # cellule vide, toujours "O"/"N"/"/"). Confirmé
                        # réel pour le niveau région (0/501 sur le lot
                        # déjà traité), pas juste théorique.
                        ecrire_rup(ws_rup, row, niveau, lien="/", texte="/")
                        continue
                    codes = "; ".join(sorted({i.algplanid for i in infos if i.algplanid}))
                    ecrire_rup(ws_rup, row, niveau, lien=infos[0].fichelink, texte=codes)
                suivi_reconciliation_rup.append((row, a.x, a.y))
            sauvegarder(wb, excel_path)
            wb = charger_classeur(excel_path)
            ws = feuille_principale(wb)
            ws_rup = feuille_rup(wb)
            n_ecrites_rue += 1
            n_total_ecrites += 1

        _logger.info("'%s' : %d nouvelle(s) ligne(s) écrite(s).", rue, n_ecrites_rue)

    # -- Réconciliation des colonnes RUP dynamiques (une par type de zone
    # rencontré, ex. "Zone voor lokaal bedrijventerrein") -----------------
    # Demande explicite de l'utilisateur (2026-09-17) : `svnaam` sert
    # directement de nom de colonne, SANS préfixe de niveau (choix
    # délibéré, matche le processus manuel habituel -- risque accepté
    # qu'un même nom de zone à deux niveaux différents partage la même
    # colonne, détecté et signalé en fin de run ci-dessous plutôt que
    # silencieux). Faite en DEUX passes sur tout le lot de CE run (voir
    # suivi_reconciliation_rup) : (1) créer toutes les colonnes
    # nécessaires, (2) écrire "O"/"N" partout -- jamais les deux en même
    # temps, sinon une colonne créée en traitant la ligne 300 resterait
    # vide pour les lignes 1->299 déjà passées.
    if suivi_reconciliation_rup:
        wb = charger_classeur(excel_path)
        ws_rup = feuille_rup(wb)
        if ws_rup is not None:
            n_a_reconcilier = len(suivi_reconciliation_rup)
            _logger.info("Réconciliation des zones RUP détaillées (%d ligne(s))...", n_a_reconcilier)
            resultats = []  # (row, List[InfoRup]) -- fusion des 3 niveaux
            niveaux_par_svnaam: dict = {}  # détection de collision inter-niveaux (voir ci-dessus)
            for i, (row, x, y) in enumerate(suivi_reconciliation_rup):
                infos_ligne = []
                for niveau, methode in (
                    ("region", rup.rup_region), ("province", rup.rup_province), ("commune", rup.rup_commune),
                ):
                    infos = methode(x, y)
                    for info in infos:
                        if info.svnaam:
                            niveaux_par_svnaam.setdefault(info.svnaam, set()).add(niveau)
                    infos_ligne.extend(infos)
                resultats.append((row, infos_ligne))
                _log_progres("Réconciliation RUP", i + 1, n_a_reconcilier)

            index = index_colonnes_dynamiques_rup(ws_rup)
            for _row, infos in resultats:
                for info in infos:
                    if info.svnaam:
                        trouver_ou_creer_colonne_dynamique_rup(ws_rup, index, info.svnaam, info.legende)

            for row, infos in resultats:
                gagnantes = {index[info.svnaam] for info in infos if info.svnaam}
                ecrire_zones_dynamiques_rup(ws_rup, row, index, gagnantes)

            sauvegarder(wb, excel_path)
            _logger.info("Réconciliation RUP terminée : %d colonne(s) de zone dynamique au total.", len(index))

            collisions = {s: n for s, n in niveaux_par_svnaam.items() if len(n) > 1}
            if collisions:
                _logger.warning(
                    "%d zone(s) RUP avec le MÊME nom rencontrées à PLUSIEURS niveaux différents (colonne "
                    "partagée, distinction de niveau perdue pour ces colonnes -- risque accepté) : %s",
                    len(collisions),
                    "; ".join(f"'{s}' ({'/'.join(sorted(n))})" for s, n in collisions.items()),
                )

    if incomplet:
        _logger.warning(
            "Résumé final (INCOMPLET, budget de temps atteint) : %d ligne(s) écrite(s) au total sur %d rue(s) "
            "demandée(s) -- relancer avec les MÊMES arguments pour reprendre (les lignes déjà écrites sont "
            "sautées automatiquement).",
            n_total_ecrites, len(rues),
        )
        return 75
    _logger.info("Résumé final : %d ligne(s) écrite(s) au total sur %d rue(s).", n_total_ecrites, len(rues))
    return 0


if __name__ == "__main__":
    sys.exit(main())
