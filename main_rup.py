"""Orchestrateur CLI — pipeline RUP SEUL (chantier séparé du
2026-10-07, voir le plan `je-voulais-dire-que-jazzy-hummingbird.md`).

Différences volontaires avec `main.py` (gabarit à 190 colonnes,
INCHANGÉ, jamais touché par ce script) :
- Entrée = COMMUNE SEULE, PAS de `--rues` : toutes les rues officielles
  sont découvertes via `AdressenService.lister_toutes_rues`.
- FILTRE : une parcelle n'est écrite QUE si au moins un RUP (région,
  province OU commune) s'applique réellement à son point ; sinon elle
  est comptée "exclue" et jamais écrite (changement majeur par rapport
  à main.py, qui écrit TOUJOURS une ligne par parcelle).
- Nouvelle colonne "Nom de RUP" (A), remplie une seule fois par groupe
  de lignes consécutives partageant la même identité RUP (voir
  `services/excel_service_rup.py::lire_cle_groupe`).
- Le texte légal complet ("Bestemming...") de chaque zone dynamique est
  extrait du PDF des prescriptions urbanistiques
  (`services/rup_pdf_service.py`), pas seulement un lien.

Suivi de progression PAR RUE (fichier `<excel>.rues-traitees.txt`,
une rue par ligne) : contrairement à main.py (liste de rues FINIE et
courte, fournie par l'utilisateur), une commune peut avoir des
centaines de rues officielles — sans ce suivi, une reprise après
épuisement du budget redécouvrirait TOUTES les rues depuis la
première à chaque run, sans jamais progresser au-delà des toutes
premières."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Set

import requests

import config
from services.adressen_service import AdressenService
from services.cache_service import HttpCache
from services.cadastre_service import CadastreService
from services.decouverte_geometrique_service import DecouverteGeometrique
from services.decouverte_service import decouvrir_parcelles
from services.exceptions import ApiServiceError
from services.excel_service import sauvegarder, vers_capakey_court
from services.excel_service_rup import (
    FIRST_DATA_ROW, charger_classeur, debut_groupe_existant, ecrire_identite, ecrire_nom_rup,
    ecrire_rup, ecrire_zones_dynamiques, etendre_groupe_nom_rup, feuille_rup,
    index_colonnes_dynamiques, lire_capakeys_deja_ecrits, lire_cle_groupe,
    trouver_ou_creer_colonne_dynamique, trouver_premiere_ligne_vide,
)
from services.http_client import HttpClient
from services.rup_pdf_service import RupPdfService
from services.wfs_rup_service import InfoRup, WfsRupService
from services.wegenregister_service import WegenregisterService
from utils.logger import get_logger, setup_logging
from utils.rate_limiter import RateLimiter

_logger = get_logger("main_rup")

_NIVEAUX = ("region", "province", "commune")

# Même principe que main.py (voir son commentaire détaillé) : délai de
# grâce pour la boucle d'écriture (aucun appel réseau coûteux de
# DÉCOUVERTE, seulement les 3 requêtes RUP + sauvegarde), pris sur la
# marge déjà réservée entre --budget-heures et le plafond dur du
# workflow GitHub Actions.
_GRACE_ECRITURE = timedelta(minutes=15)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pipeline RUP seul (Flandre) — entrée commune seule.")
    parser.add_argument("--commune", required=True)
    parser.add_argument("--code-postal", required=True)
    parser.add_argument("--template", default=str(config.TEMPLATE_RUP_PATH))
    parser.add_argument("--state-dir", default=str(config.STATE_DIR))
    parser.add_argument("--cache-dir", default=str(config.CACHE_DIR))
    parser.add_argument("--logs-dir", default=str(config.BASE_DIR / "logs"))
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--sans-decouverte-geometrique", action="store_true",
        help="Désactive la découverte géométrique (parcelles sans adresse propre bordant la rue).",
    )
    parser.add_argument("--rayon-geometrique-m", type=float, default=10.0)
    parser.add_argument(
        "--budget-heures", type=float, default=5.5,
        help="Budget de temps interne (heures) -- voir _GRACE_ECRITURE et main.py pour la justification complète.",
    )
    return parser.parse_args(argv)


def chemin_etat_commune(state_dir: Path, commune: str, code_postal: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", commune.strip().lower()).strip("-")
    return state_dir / f"{code_postal}_{slug}_rup.xlsx"


def chemin_rues_traitees(excel_path: Path) -> Path:
    return excel_path.with_name(f"{excel_path.stem}.rues-traitees.txt")


def lire_rues_traitees(chemin: Path) -> Set[str]:
    if not chemin.exists():
        return set()
    return {ligne.strip() for ligne in chemin.read_text(encoding="utf-8").splitlines() if ligne.strip()}


def marquer_rue_traitee(chemin: Path, rue: str) -> None:
    with chemin.open("a", encoding="utf-8") as f:
        f.write(rue + "\n")


def _log_progres(prefixe: str, actuel: int, total: int, largeur: int = 30) -> None:
    if total <= 0:
        return
    palier = max(1, total // 20)
    if actuel != 1 and actuel != total and actuel % palier != 0:
        return
    pct = actuel / total
    n_rempli = int(largeur * pct)
    barre = "#" * n_rempli + "-" * (largeur - n_rempli)
    _logger.info("%s [%s] %3d%% (%d/%d)", prefixe, barre, int(pct * 100), actuel, total)


def _infos_rup_parcelle(rup: WfsRupService, x: float, y: float) -> Optional[dict]:
    """Interroge les 3 niveaux RUP pour un point. Renvoie un dict
    niveau -> List[InfoRup] SEULEMENT si les TROIS niveaux ont pu être
    résolus (même si certains sont des listes vides) ; `None` si au
    MOINS un niveau a levé une erreur réseau/API -- jamais une
    exclusion de la parcelle sur la base d'une incertitude : `None`
    signifie "à retenter au prochain run", jamais "confirmé sans RUP"
    (voir la discipline du projet, main.py et resolveur_service.py)."""
    resultat: dict = {}
    for niveau, methode in (("region", rup.rup_region), ("province", rup.rup_province), ("commune", rup.rup_commune)):
        try:
            resultat[niveau] = methode(x, y)
        except (requests.exceptions.RequestException, ApiServiceError) as exc:
            _logger.warning("RUP %s indisponible (x=%s, y=%s) : %s -- parcelle reportée.", niveau, x, y, exc)
            return None
    return resultat


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(Path(args.logs_dir), debug=args.debug)

    cache = HttpCache(Path(args.cache_dir))
    rate_limiter = RateLimiter(config.MAX_REQUESTS_PER_SECOND)
    http = HttpClient(cache, rate_limiter, timeout=config.HTTP_TIMEOUT_SECONDS)

    adressen = AdressenService(http)
    cadastre = CadastreService(http)
    geometrique = (
        DecouverteGeometrique(WegenregisterService(http), cadastre, rayon_m=args.rayon_geometrique_m)
        if not args.sans_decouverte_geometrique else None
    )
    rup = WfsRupService(http)
    rup_pdf = RupPdfService(Path(args.cache_dir) / "rup_pdf")

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    excel_path = chemin_etat_commune(state_dir, args.commune, args.code_postal)
    if not excel_path.exists():
        _logger.info("Aucun état existant pour '%s', amorçage depuis le gabarit RUP (%s).", args.commune, excel_path)
        shutil.copyfile(Path(args.template), excel_path)

    chemin_traitees = chemin_rues_traitees(excel_path)
    rues_traitees = lire_rues_traitees(chemin_traitees)

    _logger.info("Découverte de TOUTES les rues officielles de '%s'...", args.commune)
    toutes_rues = adressen.lister_toutes_rues(args.commune)
    rues_a_faire = [r for r in toutes_rues if r not in rues_traitees]
    _logger.info(
        "%d rue(s) officielle(s) au total, %d déjà entièrement traitée(s), %d restante(s).",
        len(toutes_rues), len(rues_traitees), len(rues_a_faire),
    )

    deadline = datetime.now(timezone.utc) + timedelta(hours=args.budget_heures)
    deadline_ecriture = deadline + _GRACE_ECRITURE
    incomplet = False
    n_total_ecrites = 0
    n_total_exclues = 0

    wb = charger_classeur(excel_path)
    ws = feuille_rup(wb)
    deja_ecrits = lire_capakeys_deja_ecrits(ws)
    derniere_ligne = trouver_premiere_ligne_vide(ws) - 1
    groupe_cle = lire_cle_groupe(ws, derniere_ligne)
    groupe_debut = debut_groupe_existant(ws, derniere_ligne) if derniere_ligne >= FIRST_DATA_ROW else None
    index_dyn = index_colonnes_dynamiques(ws)

    for rue in rues_a_faire:
        if datetime.now(timezone.utc) >= deadline:
            _logger.warning(
                "Budget de temps (%.1fh) atteint avant '%s' -- rue (et toutes les suivantes) reportée(s) "
                "au prochain run.", args.budget_heures, rue,
            )
            incomplet = True
            break

        _logger.info("Découverte de '%s' (%s)...", rue, args.commune)
        try:
            parcelles = decouvrir_parcelles(args.commune, rue, adressen, cadastre, geometrique, deadline=deadline)
        except Exception as exc:  # noqa: BLE001 -- une rue en échec ne doit jamais faire planter tout le run
            _logger.warning(
                "Découverte de '%s' (%s) a échoué (%s: %s) -- rue SAUTÉE pour ce run, relance le même "
                "traitement plus tard pour la retenter.", rue, args.commune, type(exc).__name__, exc,
            )
            continue
        _logger.info("'%s' : %d parcelle(s) à vérifier.", rue, len(parcelles))

        rue_complete = True
        n_ecrites_rue = 0
        n_exclues_rue = 0
        for i_parcelle, p in enumerate(parcelles):
            _log_progres(f"'{rue}'", i_parcelle + 1, len(parcelles))
            capakey_court = vers_capakey_court(p.parcelle.reference)
            if capakey_court in deja_ecrits:
                continue
            if datetime.now(timezone.utc) >= deadline_ecriture:
                _logger.warning(
                    "Budget (+grâce) atteint EN COURS de '%s' -- %d/%d parcelle(s) traitée(s), le reste "
                    "(et les rues suivantes) reporté au prochain run.",
                    rue, n_ecrites_rue + n_exclues_rue, len(parcelles),
                )
                incomplet = True
                rue_complete = False
                break

            a = p.adresses[0]
            code_postal_ligne = a.postcode or args.code_postal
            infos_par_niveau = _infos_rup_parcelle(rup, a.x, a.y)
            if infos_par_niveau is None:
                rue_complete = False  # erreur transitoire -- cette rue sera redécouverte au prochain run
                continue
            toutes_infos: List[InfoRup] = [i for niveau in _NIVEAUX for i in infos_par_niveau[niveau]]
            if not toutes_infos:
                n_exclues_rue += 1
                continue  # confirmé : aucun RUP à aucun niveau -- hors scope, jamais écrite (demande du 2026-10-07)

            row = trouver_premiere_ligne_vide(ws)
            ecrire_identite(
                ws, row, commune=args.commune, code_postal=code_postal_ligne, rue=rue,
                numero=a.huisnummer, capakey=capakey_court,
            )

            textes_niveau = {}
            for niveau in _NIVEAUX:
                infos = infos_par_niveau[niveau]
                if not infos:
                    ecrire_rup(ws, row, niveau, lien="/", texte="/")
                    textes_niveau[niveau] = "/"
                    continue
                codes = "; ".join(sorted({i.algplanid for i in infos if i.algplanid}))
                ecrire_rup(ws, row, niveau, lien=infos[0].fichelink, texte=codes)
                textes_niveau[niveau] = codes

            cle_courante = (textes_niveau["region"], textes_niveau["province"], textes_niveau["commune"])
            if groupe_debut is not None and cle_courante == groupe_cle:
                etendre_groupe_nom_rup(ws, groupe_debut, row)
            else:
                noms = sorted({i.naam for i in toutes_infos if i.naam})
                ecrire_nom_rup(ws, row, "; ".join(noms) if noms else "/")
                groupe_cle = cle_courante
                groupe_debut = row

            for info in toutes_infos:
                if info.svnaam and info.svnaam not in index_dyn:
                    bestemming = rup_pdf.extraire_bestemming(info.svidlink) if info.svidlink else None
                    trouver_ou_creer_colonne_dynamique(ws, index_dyn, info.svnaam, info.legende, bestemming or "")
            gagnantes = {index_dyn[i.svnaam] for i in toutes_infos if i.svnaam}
            ecrire_zones_dynamiques(ws, row, index_dyn, gagnantes)

            sauvegarder(wb, excel_path)
            wb = charger_classeur(excel_path)
            ws = feuille_rup(wb)
            deja_ecrits.add(capakey_court)
            n_ecrites_rue += 1
            n_total_ecrites += 1

        n_total_exclues += n_exclues_rue
        _logger.info(
            "'%s' : %d ligne(s) écrite(s), %d parcelle(s) exclue(s) (aucun RUP applicable).",
            rue, n_ecrites_rue, n_exclues_rue,
        )
        if rue_complete:
            marquer_rue_traitee(chemin_traitees, rue)
        else:
            incomplet = True

    if incomplet:
        _logger.warning(
            "Résumé final (INCOMPLET, budget de temps atteint ou erreur transitoire) : %d ligne(s) écrite(s), "
            "%d parcelle(s) exclue(s) -- relance avec les MÊMES arguments pour continuer (rues et parcelles "
            "déjà traitées sautées automatiquement).", n_total_ecrites, n_total_exclues,
        )
        return 75
    _logger.info(
        "Résumé final : %d ligne(s) écrite(s), %d parcelle(s) exclue(s) sur %d rue(s).",
        n_total_ecrites, n_total_exclues, len(toutes_rues),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
