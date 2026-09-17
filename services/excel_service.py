"""Lecture/écriture du classeur Excel flamand — BEAUCOUP plus simple que
côté France : pas de registre de colonnes dynamique par icône (le
gabarit n'a aucune image ancrée, `models/colonnes_be.py` donne un
mapping FIXE lettre->rôle), donc pas de `scan_layout` à refaire à
chaque fichier.

Structure confirmée en direct sur le gabarit officiel réel ("Formation
Geopunt 1 1.xlsx", reçu le 2026-09-16, DEUX feuilles) :
- Feuille principale (nom arbitraire, ex. "Feuille"/"Mesen" selon le
  fichier -- jamais fixe, toujours prise par POSITION, `wb.worksheets[0]`)
  : ligne 2 = en-têtes identité (A->E), ligne 3 = catégorie (dynamique),
  ligne 4 = palier, ligne 5+ = données.
- Feuille **"RUP"** (nom stable, cherché par nom) : même structure
  d'identité (A->E), mais seulement 6 colonnes utiles (F/G, I/J, L/M --
  lien + texte libre par niveau région/province/commune), à compléter
  MANUELLEMENT par l'utilisateur (voir le plan : le bloc RUP détaillé
  de la feuille principale reçoit "/" partout, jamais de classement
  automatique construit)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Set

import openpyxl
from openpyxl.utils import column_index_from_string
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from models.colonnes_be import COLONNES_BE
from utils.logger import get_logger

_logger = get_logger("services.excel_service")

# Format court du numéro cadastral affiché sur la carte geopunt.be (ex.
# "233D", "9") -- demande explicite de l'utilisateur du 2026-09-17,
# confirmée par capture d'écran de la carte, PLUS FIABLE que le format
# CaPaKey complet renvoyé par le WFS fédéral (71053H1081/00_000)
# uniquement pour la LECTURE humaine du fichier final -- le format
# complet reste la seule référence fiable en interne (une vraie
# collision existe déjà entre deux communes différentes : "149L" =
# Gippershovenstraat 1 (Sint-Truiden) ET Gorsem-Dorp 89 (rattachée à un
# code cadastral historique différent, 71018), voir la conversation).
_RE_CAPAKEY_COMPLET = re.compile(r"^(\d{5})([A-Z])(\d{4})/(\d{2})([A-Z_])(\d{3})$")


def vers_capakey_court(reference: str) -> str:
    """Convertit une référence cadastrale COMPLÈTE (71053H1081/00_000,
    telle que renvoyée par le WFS fédéral -- voir cadastre_service.py)
    vers le format court affiché sur geopunt.be (1081, 1049B, 59B...).
    Renvoie `reference` INCHANGÉE si le motif attendu n'est pas
    reconnu -- jamais d'exception qui ferait échouer tout le traitement
    de la parcelle pour un simple souci de présentation."""
    m = _RE_CAPAKEY_COMPLET.match(reference)
    if not m:
        _logger.warning("Référence cadastrale '%s' : motif inattendu, conversion au format court ignorée.", reference)
        return reference
    _nis, _section, parcel, _bisnum, bisletter, power = m.groups()
    court = str(int(parcel))
    if bisletter != "_":
        court += bisletter
    if power != "000":
        court += str(int(power))
    return court

HEADER_ROW = 2
FIRST_DATA_ROW = 5

COL_COMMUNE = 1  # A
COL_CODE_POSTAL = 2  # B
COL_RUE = 3  # C
COL_NUMERO = 4  # D
COL_NUMERO_CADASTRAL = 5  # E

NOM_FEUILLE_RUP = "RUP"


def charger_classeur(excel_path: Path) -> Workbook:
    return openpyxl.load_workbook(excel_path)


def feuille_principale(wb: Workbook) -> Worksheet:
    """Toujours la PREMIÈRE feuille par position -- son nom varie d'un
    fichier à l'autre (vu "Mesen" puis "Feuille" sur deux exemplaires du
    même gabarit), jamais fiable à chercher par nom."""
    return wb.worksheets[0]


def feuille_rup(wb: Workbook) -> Optional[Worksheet]:
    """La feuille "RUP" (nom stable) si le classeur en a une -- `None`
    sur un ancien gabarit à une seule feuille, jamais deviné."""
    return wb[NOM_FEUILLE_RUP] if NOM_FEUILLE_RUP in wb.sheetnames else None


def charger_feuille(excel_path: Path) -> Worksheet:
    """Compatibilité : renvoie directement la feuille principale."""
    return feuille_principale(charger_classeur(excel_path))


def trouver_premiere_ligne_vide(ws: Worksheet) -> int:
    """Première ligne (>= FIRST_DATA_ROW) où la colonne A (Commune) est
    vide — même convention que côté France."""
    r = FIRST_DATA_ROW
    while ws.cell(row=r, column=COL_COMMUNE).value not in (None, ""):
        r += 1
    return r


def lire_capakeys_deja_ecrits(ws: Worksheet) -> Set[str]:
    """Référence cadastrale (colonne E) de chaque ligne déjà écrite —
    utilisé pour ne jamais réécrire une parcelle déjà traitée (motif
    "continuer" de reprise, même principe que côté France)."""
    capakeys: Set[str] = set()
    derniere = trouver_premiere_ligne_vide(ws) - 1
    for r in range(FIRST_DATA_ROW, derniere + 1):
        v = ws.cell(row=r, column=COL_NUMERO_CADASTRAL).value
        if v not in (None, ""):
            capakeys.add(str(v).strip())
    return capakeys


def ecrire_identite(ws: Worksheet, row: int, *, commune: str, code_postal: str, rue: str, numero: str, capakey: str) -> None:
    ws.cell(row=row, column=COL_COMMUNE, value=commune)
    ws.cell(row=row, column=COL_CODE_POSTAL, value=code_postal)
    ws.cell(row=row, column=COL_RUE, value=rue)
    ws.cell(row=row, column=COL_NUMERO, value=numero)
    ws.cell(row=row, column=COL_NUMERO_CADASTRAL, value=capakey)


# Colonnes (lien, texte) par niveau sur la feuille "RUP" — voir
# wfs_rup_service.py, confirmé en direct sur la structure réelle de
# cette feuille (F/G région, I/J province, L/M commune).
_RUP_COLONNES = {"region": (6, 7), "province": (9, 10), "commune": (12, 13)}


def ecrire_rup(ws_rup: Worksheet, row: int, niveau: str, *, lien: str, texte: str) -> None:
    """Écrit le lien + texte libre RUP pour `niveau`
    ("region"/"province"/"commune") à la ligne `row` de la feuille RUP —
    jamais de classement, juste ce que l'API a réellement renvoyé pour
    ce point (voir `services/wfs_rup_service.py::InfoRup`)."""
    col_lien, col_texte = _RUP_COLONNES[niveau]
    ws_rup.cell(row=row, column=col_lien, value=lien)
    ws_rup.cell(row=row, column=col_texte, value=texte)


def ecrire_ligne(
    ws: Worksheet, row: int, *, commune: str, code_postal: str, rue: str,
    numero: str, capakey: str, valeurs: Dict[str, str],
) -> None:
    """Écrit l'identité (A->E) puis les valeurs dynamiques (`valeurs` :
    lettre de colonne Excel -> "O"/"N"/"/"/texte) à la ligne `row` de la
    feuille PRINCIPALE. Ne valide PAS que chaque lettre de `valeurs`
    existe dans `COLONNES_BE` (les appelants passent déjà des lettres
    résolues via les services WFS) -- journalise un avertissement
    plutôt que planter si une lettre inattendue apparaît, jamais deviné
    où l'écrire."""
    ecrire_identite(ws, row, commune=commune, code_postal=code_postal, rue=rue, numero=numero, capakey=capakey)

    for lettre, valeur in valeurs.items():
        if lettre not in COLONNES_BE:
            _logger.warning(
                "Colonne '%s' absente de COLONNES_BE (ligne %d, parcelle %s) -- valeur '%s' NON écrite.",
                lettre, row, capakey, valeur,
            )
            continue
        idx = column_index_from_string(lettre)
        ws.cell(row=row, column=idx, value=valeur)


def sauvegarder(wb: Workbook, excel_path: Path) -> None:
    wb.save(excel_path)
