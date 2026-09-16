"""Lecture/écriture du classeur Excel flamand — BEAUCOUP plus simple que
côté France (`services/excel_service.py` du repo France) : pas de
registre de colonnes dynamique par icône (le gabarit vu n'a aucune image
ancrée, `models/colonnes_be.py` donne un mapping FIXE lettre->rôle),
donc pas de `scan_layout` à refaire à chaque fichier.

Structure confirmée en direct sur le gabarit ("Formation Geopunt
1.xlsx", feuille "Mesen") : ligne 2 = en-têtes identité (A->E), ligne 3
= catégorie (dynamique), ligne 4 = palier, ligne 5+ = données. Une
seule feuille, aucune image."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Set

import openpyxl
from openpyxl.utils import column_index_from_string
from openpyxl.worksheet.worksheet import Worksheet

from models.colonnes_be import COLONNES_BE
from utils.logger import get_logger

_logger = get_logger("services.excel_service")

HEADER_ROW = 2
FIRST_DATA_ROW = 5

COL_COMMUNE = 1  # A
COL_CODE_POSTAL = 2  # B
COL_RUE = 3  # C
COL_NUMERO = 4  # D
COL_NUMERO_CADASTRAL = 5  # E


def charger_feuille(excel_path: Path) -> Worksheet:
    wb = openpyxl.load_workbook(excel_path)
    return wb.active


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


def ecrire_ligne(
    ws: Worksheet, row: int, *, commune: str, code_postal: str, rue: str,
    numero: str, capakey: str, valeurs: Dict[str, str],
) -> None:
    """Écrit l'identité (A->E) puis les valeurs dynamiques (`valeurs` :
    lettre de colonne Excel -> "O"/"N"/texte) à la ligne `row`. Ne
    valide PAS que chaque lettre de `valeurs` existe dans
    `COLONNES_BE` (les appelants passent déjà des lettres résolues via
    les services WFS) -- journalise un avertissement plutôt que planter
    si une lettre inattendue apparaît, jamais deviné où l'écrire."""
    ws.cell(row=row, column=COL_COMMUNE, value=commune)
    ws.cell(row=row, column=COL_CODE_POSTAL, value=code_postal)
    ws.cell(row=row, column=COL_RUE, value=rue)
    ws.cell(row=row, column=COL_NUMERO, value=numero)
    ws.cell(row=row, column=COL_NUMERO_CADASTRAL, value=capakey)

    for lettre, valeur in valeurs.items():
        if lettre not in COLONNES_BE:
            _logger.warning(
                "Colonne '%s' absente de COLONNES_BE (ligne %d, parcelle %s) -- valeur '%s' NON écrite.",
                lettre, row, capakey, valeur,
            )
            continue
        idx = column_index_from_string(lettre)
        ws.cell(row=row, column=idx, value=valeur)


def sauvegarder(ws: Worksheet, excel_path: Path) -> None:
    ws.parent.save(excel_path)
