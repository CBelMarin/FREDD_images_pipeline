#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline_fredd.py
=================

Pipeline complet de traitement des images FREDD, en 5 étapes :

  1. INVENTAIRE      de toutes les images (DICOM, E2E, images standards) d'un
                     ou plusieurs dossiers, y compris dans des .zip imbriqués
                     (réutilise inventaire_images.py, inchangé).
  2. MISE À L'ÉCART  des images dont le pseudonyme n'est pas dans les données
                     FREDD (CSV) -> sortie/sans_patient/<centre>/...
  3. PRÉTRAITEMENT   par le module pipeline_wp2 (raretia : recadrage,
                     débruitage, CLAHE, standardisation, détection, contrôle
                     qualité, écriture DICOM pseudonymisé), puis rangement
                     -> sortie/images_traitees/<centre>/<date_traitement>/<id_patient>/...
                     Un « centre » = sous-dossier de 1er niveau d'un dossier
                     scanné (ou archive .zip posée à la racine).
  4. RAPPORT D'EXÉCUTION complet (journal par image, sorties, bilan par
                     centre, rapport lisible .md).
  5. CONFRONTATION   avec les données : pour chaque patient, images déclarées
                     dans SKEZIA (colonnes du CSV) vs images retrouvées.

PRINCIPES
  - Les fichiers d'origine ne sont JAMAIS modifiés, déplacés ni supprimés :
    tout est COPIÉ vers le dossier de sortie.
  - Rien ne disparaît silencieusement : chaque image inventoriée a exactement
    une ligne dans rapports/journal_images.csv, avec un statut.
  - Chaque DICOM produit par raretia est contrôlé (PatientName vide,
    PatientID = pseudonyme attendu, aucun identifiant source résiduel) ;
    en cas d'échec il va en quarantaine, pas dans images_traitees.

UTILISATION
  python pipeline_fredd.py "D:\\FREDD\\images" --data "D:\\FREDD\\data_clean.csv" ^
         --sortie "D:\\FREDD\\sortie"
  (le module pipeline_wp2/ est livré à côté de ce script et utilisé par défaut)
  python pipeline_fredd.py ... --simulation      (aucune copie, aucun traitement)

Voir README.md et `python pipeline_fredd.py --help`.
"""

import argparse
import csv
import io
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

import inventaire_images as inv
import analyse_contenu as ac
import raretia_adapter as ra
import controle_texte as ct
import echantillon as ech_mod

__version__ = "1.12.6"

try:
    import pydicom  # nécessaire pour le contrôle de pseudonymisation
except ImportError:  # pragma: no cover
    pydicom = None


STATUTS = OrderedDict([
    ("TRAITEE_OK", "Traitée par raretia, qualité acceptée"),
    ("TRAITEE_QUALITE_LIMITEE", "Traitée par raretia, marquée « QUALITÉ LIMITÉE » (obstruction sévère de la zone utile)"),
    ("TRAITEE_QUALITE_INCONNUE", "Traitée par raretia, statut qualité absent du rapport raretia"),
    ("ECHEC_TRAITEMENT", "raretia n'a produit aucune image (copie dans echecs_traitement/)"),
    ("ECHEC_PSEUDONYMISATION", "Toutes les sorties sont non conformes à la pseudonymisation "
                               "(quarantaine_pseudonymisation/)"),
    ("QUARANTAINE_PARTIELLE", "Une partie seulement des sorties est non conforme : celles-là sont en "
                              "quarantaine_pseudonymisation/, les autres sont livrées dans images_traitees/"),
    ("ECHEC_EXTRACTION", "Image illisible au moment de la copie (zip / disque)"),
    ("MISE_DE_COTE_SANS_PATIENT", "Pseudonyme détecté mais absent des données FREDD (sans_patient/)"),
    ("MISE_DE_COTE_ID_NON_DETECTE", "Aucun pseudonyme détecté dans le chemin (sans_patient/_id_non_detecte/)"),
    ("MISE_DE_COTE_ID_AMBIGU", "ID ambigu : plusieurs patients FREDD, ou ID différents dossier/fichier "
                               "(sans_patient/_id_ambigu/)"),
    ("NON_TRAITEE_FORMAT", "Patient FREDD mais format non pris en charge par raretia (non_traitees_format/)"),
    ("NON_TRAITEE_IR", "E2E ne contenant que de l'infrarouge : IR exclues par décision FREDD "
                       "(non_traitees_format/<centre>/_infrarouge/)"),
    ("NON_TRAITEE_RAW_DATA", "DICOM « Raw Data » : données propriétaires de l'appareil, sans image affichable ; "
                             "ignoré par raretia (non_traitees_format/<centre>/_raw_data/)"),
    ("NON_TRAITEE_MONTAGE", "Montage ultra-grand-champ reconstruit (Zeiss CLARUS) dont toutes les images natives sont "
                            "présentes : seules les natives sont traitées (non_traitees_format/<centre>/_montage_reconstruit/)"),
    ("NON_TRAITEE_ANGIO", "Angiographie (fluorescéine ou ICG) : hors périmètre FREDD, non envoyée à raretia "
                          "(non_traitees_format/<centre>/_angiographie/)"),
    ("NON_TRAITEE_DICOMDIR", "Fichier DICOMDIR : index d'un support (CD/DVD), aucune image ; "
                             "non envoyé à raretia (non_traitees_format/<centre>/_dicomdir/)"),
    ("IGNOREE_DOUBLON", "Copie binaire identique (SHA-256) d'une image déjà prise en charge pour ce patient"),
    ("A_TRAITER_SIMULATION", "Serait envoyée à raretia (mode --simulation)"),
    ("HORS_ECHANTILLON", "Patient FREDD hors de l'échantillon choisi (--echantillon) : non traitée dans cette exécution"),
])

STATUTS_TRAITES = {"TRAITEE_OK", "TRAITEE_QUALITE_LIMITEE", "TRAITEE_QUALITE_INCONNUE", "QUARANTAINE_PARTIELLE"}
CATEGORIES_RARETIA = {"DICOM": ".dcm", "E2E_HEIDELBERG": ".e2e"}

JOURNAL_HEADERS = [
    "chemin_source", "racine_scan", "lot", "centre", "categorie", "format_detecte", "contenu_detecte",
    "patient_id_detecte", "id_fredd", "statut", "libelle_statut", "destination",
    "nb_sorties", "nb_sorties_livrees", "nb_sorties_quarantaine",
    "types_sorties", "score_min", "motif", "observation", "sha256",
]
SORTIES_HEADERS = [
    "centre", "id_fredd", "fichier_sortie", "chemin_source", "image_type", "lateralite", "orientation_coupe", "champ",
    "largeur_px", "hauteur_px", "pct_pixels_utiles",
    "score_qualite", "acceptee", "problemes", "motif", "pseudonymisation", "rangement",
]
CONFRONTATION_HEADERS_BASE = [
    "id_fredd", "present_donnees_fredd", "centre_donnees", "centres_images", "centre_coherent",
    "images_declarees_skezia", "detail_declaration_skezia", "nb_images_inventoriees",
    "nb_images_traitees", "nb_qualite_limitee", "nb_echecs", "nb_mises_de_cote", "nb_images_id_discordant",
    "contenu_trouve", "types_images_traitees",
    "statut_confrontation", "nb_ecarts_modalite", "detail_par_modalite",
]

CONFRONTATION_LIBELLES = OrderedDict([
    ("SKEZIA_ET_FREDD", "Images déclarées dans SKEZIA et retrouvées dans les dossiers FREDD"),
    ("SKEZIA_SEULEMENT", "Images déclarées dans SKEZIA mais AUCUNE retrouvée dans les dossiers"),
    ("FREDD_SEULEMENT", "Images retrouvées dans les dossiers mais NON déclarées dans SKEZIA"),
    ("NI_SKEZIA_NI_FREDD", "Patient sans image déclarée ni retrouvée"),
    ("IMAGES_HORS_DONNEES", "Images retrouvées pour un pseudonyme absent des données FREDD"),
    ("PRESENT_AVEC_IMAGES", "Patient FREDD avec images (colonnes SKEZIA non renseignées)"),
    ("PRESENT_SANS_IMAGE", "Patient FREDD sans image (colonnes SKEZIA non renseignées)"),
])

# Correspondance CRF FREDD (page_depot_images, dictionnaire Survey_Lille.csv) ->
# types d'images raretia et œil. Activée par --cols-skezia fredd.
#   colonne : type raretia (préfixe) : œil
PRESET_SKEZIA_FREDD = ",".join([
    "dep_FO_dep_dispo_FO_dep_dispo_FO_OD:fundus:OD",
    "dep_FO_dep_dispo_FO_dep_dispo_FO_OG:fundus:OG",
    # DÉFINITION RETENUE (décision FREDD) : la colonne « mac » = coupe HORIZONTALE, la colonne
    # « temp » = coupe VERTICALE, quel que soit le libellé historique du CRF (« ligne maculaire »,
    # « coupe temporale »...). La vérité est dans les images : les écarts sont remontés aux centres
    # (rapports/corrections_crf/) plutôt que corrigés par une table de correspondance par centre.
    # Une coupe d'une autre orientation (radiale, oblique...) relève de la colonne « autre ». Son nom ci-dessous
    # est une HYPOTHÈSE (même schéma que les autres) : s'il diffère dans le CRF, la colonne est ignorée
    # et signalée (« colonnes du préréglage absentes de l'export »). Un volume dont les lignes sont
    # horizontales (ou verticales) compte comme horizontal (ou vertical) ; sa nature de pile est notée à part.
    "dep_dispo_OCT_dep_dispo_OCT_mac_dep_dispo_OCT_OD:oct_bscan:OD:horizontale",
    "dep_dispo_OCT_dep_dispo_OCT_mac_dep_dispo_OCT_OG:oct_bscan:OG:horizontale",
    "dep_dispo_OCT_dep_dispo_OCT_temp_dep_dispo_OCT_OD:oct_bscan:OD:verticale",
    "dep_dispo_OCT_dep_dispo_OCT_temp_dep_dispo_OCT_OG:oct_bscan:OG:verticale",
    "dep_dispo_OCT_dep_dispo_OCT_autre_dep_dispo_OCT_OD:oct_bscan:OD:autre",
    "dep_dispo_OCT_dep_dispo_OCT_autre_dep_dispo_OCT_OG:oct_bscan:OG:autre",
    # AF : « mac » = champ maculaire (Heidelberg 30°), « gc » = grand champ (55°). Le champ n'est lisible qu'après traitement (règle des 4 coins, pixels) :
    # sans passage dans raretia (--simulation), ces deux lignes sont « NON_EVALUABLE ».
    "dep_dispo_AF_dep_dispo_AF_mac_dep_dispo_AF_OD:autofluorescence:OD:maculaire",
    "dep_dispo_AF_dep_dispo_AF_mac_dep_dispo_AF_OG:autofluorescence:OG:maculaire",
    "dep_dispo_AF_dep_dispo_AF_gc_dep_dispo_AF_OD:autofluorescence:OD:grand_champ",
    "dep_dispo_AF_dep_dispo_AF_gc_dep_dispo_AF_OG:autofluorescence:OG:grand_champ",
])

# Contrôles complémentaires du CRF : appareil déclaré (dep_app_FO_*, dep_app_AF_*)
# et « AF dans le même fichier E2E que l'OCT » (dep_AF_OCT). Colonnes facultatives : absentes, elles sont signalées.
PRESET_APPAREILS = ["dep_app_FO_OD:fundus:OD", "dep_app_FO_OG:fundus:OG",
                    "dep_app_AF_OD:autofluorescence:OD", "dep_app_AF_OG:autofluorescence:OG"]
COL_AF_OCT = "dep_AF_OCT"
# Codes du CRF : fond d'œil 1 Clarus, 2 Optos, 3 Autre ; AF 1 Clarus, 2 Optos, 3 Heidelberg, 4 Autre
_CODES_APPAREIL = {"fundus": {"1": "clarus", "2": "optos", "3": "autre"},
                   "autofluorescence": {"1": "clarus", "2": "optos", "3": "heidelberg", "4": "autre"}}
_APPAREILS_CONNUS = {"clarus", "optos", "heidelberg"}


def appareil_declare(v, type_image):
    """Famille d'appareil déclarée dans le CRF (code ou libellé) : clarus | optos | heidelberg | autre | '' (vide)."""
    s = ascii_lower(v)
    if not s:
        return ""
    if s in _CODES_APPAREIL.get(type_image, {}):
        return _CODES_APPAREIL[type_image][s]
    for f in ("clarus", "optos", "heidelberg"):
        if f in s:
            return f
    return "autre" if "autre" in s or "other" in s else ""


def appareil_trouve(texte):
    """Famille d'appareil lue dans les images (fabricant + modèle) : clarus | optos | heidelberg | autre | ''."""
    s = ascii_lower(texte)
    if not s:
        return ""
    if "clarus" in s:
        return "clarus"
    if "optos" in s:
        return "optos"
    if "heidelberg" in s or "spectralis" in s:
        return "heidelberg"
    return "autre"


_OEIL = {"od": "OD", "r": "OD", "right": "OD", "droit": "OD",
         "og": "OG", "os": "OG", "l": "OG", "left": "OG", "gauche": "OG"}


def norm_oeil(v):
    """Latéralité DICOM / CRF -> 'OD', 'OG' ou '' (inconnue)."""
    return _OEIL.get(ascii_lower(v), "")


def _norm_col(s):
    """Nom de colonne comparable : casse, points/tirets/espaces ignorés
    (un export peut écrire 'dep_FO.dep_dispo_FO.dep_dispo_FO_OD')."""
    return re.sub(r"[.\-\s]+", "_", ascii_lower(s))


ID_COLUMN_CANDIDATES = ["id", "patient_id", "adm_identifiant_bamara", "pseudonyme", "pseudo",
                        "identifiant", "id_patient", "record_id"]
CENTRE_COLUMN_CANDIDATES = ["centre", "center", "site", "adm_centre", "centre_inclusion", "hopital"]

_VRAI = {"oui", "o", "yes", "y", "1", "true", "vrai", "x", "present", "presente", "fait", "done", "checked"}
_FAUX = {"", "non", "n", "no", "0", "false", "faux", "na", "nan", "nd", "none", "absent", "absente",
         "unchecked", "-", "."}


def ascii_lower(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii").strip().lower()


def valeur_declaree(v):
    """Interprète une cellule SKEZIA : True (déclarée), False, ou None (vide)."""
    s = ascii_lower(v)
    if s in _FAUX:
        return None if s in {"", "na", "nan", "nd", "none", "-", "."} else False
    if s in _VRAI:
        return True
    try:
        return float(s.replace(",", ".")) > 0
    except ValueError:
        return True  # valeur non vide et non négative (ex. une date d'examen) => déclarée


_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s, maxlen=120):
    s = _INVALID.sub("_", str(s)).strip().rstrip(". ")
    if s in ("", ".", ".."):
        s = "_"
    return s[:maxlen]


def centre_depuis_lot(lot):
    """'racine/Centre_A' -> 'Centre_A' ; 'racine/Centre_B (zip)' -> 'Centre_B'."""
    nom = lot.split("/", 1)[1] if "/" in lot else lot
    if nom == "(racine)":
        return "_centre_inconnu"
    if nom.endswith(" (zip)"):
        nom = nom[: -len(" (zip)")]
    return nom


def parties_relatives(chemin, racine):
    """Chemin relatif (liste de composants sûrs) d'une image sous sa racine,
    zips compris : 'R/C/a.zip :: x/y.dcm' -> ['C', 'a.zip', 'x', 'y.dcm']."""
    morceaux = chemin.split(" :: ")
    try:
        rel = os.path.relpath(morceaux[0], racine)
    except ValueError:
        rel = os.path.basename(morceaux[0])
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    for m in morceaux[1:]:
        parts.extend(p for p in m.replace("\\", "/").split("/") if p not in ("", ".", ".."))
    return [safe_name(p) for p in parts] or ["_"]


_PREFIXE_ID = re.compile(
    r"^([0-9a-f]{8})(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?(?![0-9a-f])", re.I)
# Repli hors convention : UUID ou bloc de 8 hexadécimaux isolé n'importe où
# dans un nom (le '_' compte comme séparateur, contrairement au \b de
# l'heuristique historique de inventaire_images).
_ID_LIBRE = re.compile(
    r"(?<![0-9a-z])([0-9a-f]{8})(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?(?![0-9a-z])", re.I)


def _est_date(s):
    if not s.isdigit() or len(s) != 8:
        return False
    a, m, j = int(s[:4]), int(s[4:6]), int(s[6:8])
    return 1900 <= a <= 2100 and 1 <= m <= 12 and 1 <= j <= 31


def id_en_prefixe(nom):
    """Convention FREDD : le dossier patient et chaque fichier sont renommés en
    COMMENÇANT par l'ID SKEZIA, complet (UUID) ou tronqué à 8 caractères.
    '300b72a1-c067-400a-b148-0141a13f641d_OCT.e2e' -> '300b72a1'
    '300b72a1_OD_fundus.dcm'                         -> '300b72a1'
    '20231005_OCT.dcm' (date AAAAMMJJ)               -> ''"""
    m = _PREFIXE_ID.match(nom.strip())
    if not m or _est_date(m.group(1)):
        return ""
    return m.group(1).lower()


def pseudo_sous_centre(chemin, racine):
    """Retourne (pseudonyme canonique 8 caractères, observation, discordant).

    1. Règle FREDD : ID en PRÉFIXE des noms de dossiers / fichiers situés sous
       le dossier-centre (le centre lui-même n'est jamais un pseudonyme).
       Si plusieurs ID différents apparaissent (dossier ≠ fichier), le cas est
       signalé comme discordant : on ne devine pas.
    2. Repli (aucun préfixe trouvé) : heuristique historique de
       inventaire_images (ID n'importe où dans un nom), limitée elle aussi au
       chemin sous le centre, jamais jusqu'aux dossiers situés AU-DESSUS du dossier scanné."""
    parts = parties_relatives(chemin, racine)
    if len(parts) <= 1:
        return "", "", False
    sous_centre = parts[1:]
    composants = sous_centre[:-1] + [os.path.splitext(sous_centre[-1])[0]]
    trouves = [i for i in (id_en_prefixe(c) for c in composants) if i]
    distincts = list(OrderedDict.fromkeys(trouves))
    if len(distincts) == 1:
        # Convention : le fichier lui-même doit aussi commencer par l'ID.
        obs = "" if id_en_prefixe(composants[-1]) else "fichier non renommé selon la convention (ID en début de nom)"
        return distincts[0], obs, False
    if len(distincts) > 1:
        return distincts[-1], "IDs discordants dans le chemin : " + ", ".join(distincts), True
    libres = []
    for c in reversed(composants):  # fichier d'abord, puis dossiers du plus profond au moins profond
        libres += [m.group(1).lower() for m in _ID_LIBRE.finditer(c) if not _est_date(m.group(1))]
    libres = list(OrderedDict.fromkeys(libres))
    if len(libres) == 1:
        return libres[0], "ID trouvé hors convention (pas en début de nom)", False
    if len(libres) > 1:
        return libres[0], "IDs discordants hors convention : " + ", ".join(libres), True
    return "", "", False


def chemin_unique(dest):
    dest = Path(dest)
    if not os.path.exists(inv.long_path_str(dest)):
        return dest
    i = 2
    while True:
        cand = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
        if not os.path.exists(inv.long_path_str(cand)):
            return cand
        i += 1


def ecrire_csv(path, headers, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers] if isinstance(r, dict) else r)
    return path


class LecteurSources:
    """Copie une image désignée par son chemin d'inventaire vers un fichier,
    qu'elle soit sur disque ou à l'intérieur de zips imbriqués
    ('a.zip :: b.zip :: x/y.dcm'). Garde un petit cache d'archives ouvertes
    (l'inventaire parcourt les zips dans l'ordre, donc les accès sont groupés)."""

    def __init__(self, taille_max_memoire_mo=200, cache=6):
        self.seuil = taille_max_memoire_mo * 1024 * 1024
        self.cache_max = cache
        self._zips = OrderedDict()  # tuple(chaîne) -> (ZipFile, tmp_path|None)

    def _ouvrir(self, chaine):
        if chaine in self._zips:
            self._zips.move_to_end(chaine)
            return self._zips[chaine][0]
        tmp = None
        if len(chaine) == 1:
            zf = zipfile.ZipFile(inv.long_path_str(chaine[0]))
        else:
            parent = self._ouvrir(chaine[:-1])
            info = parent.getinfo(chaine[-1])
            if info.file_size <= self.seuil:
                zf = zipfile.ZipFile(io.BytesIO(parent.read(info)))
            else:
                fd, tmp = tempfile.mkstemp(suffix=".zip")
                with os.fdopen(fd, "wb") as dst, parent.open(info) as src:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                zf = zipfile.ZipFile(tmp)
        self._zips[chaine] = (zf, tmp)
        while len(self._zips) > self.cache_max:
            _k, (old, old_tmp) = self._zips.popitem(last=False)
            self._fermer(old, old_tmp)
        return zf

    @staticmethod
    def _fermer(zf, tmp):
        try:
            zf.close()
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def copier_vers(self, chemin, dest):
        dest = Path(dest)
        os.makedirs(inv.long_path_str(dest.parent), exist_ok=True)
        morceaux = chemin.split(" :: ")
        if len(morceaux) == 1:
            shutil.copyfile(inv.long_path_str(morceaux[0]), inv.long_path_str(dest))
            return dest
        zf = self._ouvrir(tuple(morceaux[:-1]))
        with zf.open(morceaux[-1]) as src, open(inv.long_path_str(dest), "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        return dest

    def fermer(self):
        while self._zips:
            _k, (zf, tmp) = self._zips.popitem()
            self._fermer(zf, tmp)


class DonneesFredd:
    def __init__(self):
        self.patients = {}          # id canonique -> dict(id, centre, skezia{col: valeur})
        self.ambigus = defaultdict(set)  # id canonique -> ids d'origine (collision)
        self.colonnes = []
        self.col_id = None
        self.col_centre = None
        self.cols_skezia = []       # liste (colonne, type_image|None, oeil 'OD'/'OG'/None)
        self.cols_skezia_absentes = []  # colonnes du préréglage absentes de l'export
        self.cols_appareil = []     # (colonne, type_image, oeil) : appareil déclaré dans le CRF
        self.col_af_oct = None      # colonne « AF dans le même E2E que l'OCT »
        self.encodage = None
        self.lignes_sans_id = 0
        self.ids_format_inattendu = 0   # IDs ni UUID ni 8 hexa (mauvaise colonne --col-id ?)
        self.lignes_fusionnees = 0      # lignes supplémentaires d'un même patient

    @staticmethod
    def _ouvrir_texte(path):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                with open(path, "r", encoding=enc, newline="") as f:
                    return f.read(), enc
            except UnicodeDecodeError:
                continue
        raise ValueError(f"Encodage illisible : {path}")

    @classmethod
    def charger(cls, path, col_id=None, col_centre=None, cols_skezia=""):
        d = cls()
        texte, d.encodage = cls._ouvrir_texte(path)
        echantillon = texte[:4096]
        delim = ";" if echantillon.count(";") >= echantillon.count(",") else ","
        reader = csv.reader(io.StringIO(texte), delimiter=delim)
        header = [h.strip().strip('"') for h in next(reader, [])]
        d.colonnes = header
        low = {}
        for i, h in enumerate(header):
            low.setdefault(_norm_col(h), i)

        def trouver(nom, candidats, obligatoire, option):
            if nom:
                if _norm_col(nom) not in low:
                    raise ValueError(f"Colonne '{nom}' ({option}) absente du CSV. Colonnes disponibles : "
                                     + ", ".join(header))
                return header[low[_norm_col(nom)]]
            for c in candidats:
                if _norm_col(c) in low:
                    return header[low[_norm_col(c)]]
            if obligatoire:
                raise ValueError(f"Aucune colonne identifiant trouvée (essayé : {', '.join(candidats)}). "
                                 f"Préciser {option}. Colonnes disponibles : " + ", ".join(header))
            return None

        d.col_id = trouver(col_id, ID_COLUMN_CANDIDATES, True, "--col-id")
        d.col_centre = trouver(col_centre, CENTRE_COLUMN_CANDIDATES, False, "--col-centre")
        preset = (cols_skezia or "").strip().lower() == "fredd"
        specs = PRESET_SKEZIA_FREDD if preset else (cols_skezia or "")
        for spec in [s.strip() for s in specs.split(",") if s.strip()]:
            morceaux = [m.strip() for m in spec.split(":")]
            col, typ = morceaux[0], (morceaux[1] if len(morceaux) > 1 else "")
            oeil = norm_oeil(morceaux[2]) if len(morceaux) > 2 and morceaux[2] else ""
            if len(morceaux) > 2 and morceaux[2] and not oeil:
                raise ValueError(f"Œil inconnu dans '{spec}' (attendu : OD ou OG)")
            orient = ""
            if len(morceaux) > 3 and morceaux[3]:
                orient = {"h": "horizontale", "horizontale": "horizontale",
                          "v": "verticale", "verticale": "verticale",
                          "a": "autre", "autre": "autre",
                          "maculaire": "maculaire", "mac": "maculaire",
                          "grand_champ": "grand_champ", "gc": "grand_champ"}.get(ascii_lower(morceaux[3]), "")
                if not orient:
                    raise ValueError(f"Orientation inconnue dans '{spec}' (attendu : horizontale, verticale, autre, "
                                     f"maculaire ou grand_champ)")
            if preset and _norm_col(col) not in low:
                d.cols_skezia_absentes.append(col)
                continue
            vraie = trouver(col, [], True, "--cols-skezia")
            d.cols_skezia.append((vraie, typ or None, oeil or None, orient or None))
        if preset and not d.cols_skezia:
            raise ValueError("Aucune colonne du CRF FREDD (page_depot_images, ex. "
                             "dep_FO_dep_dispo_FO_dep_dispo_FO_OD) trouvée dans l'export. "
                             "Colonnes disponibles : " + ", ".join(header))

        if preset:
            for spec in PRESET_APPAREILS:
                col, typ, oeil = spec.split(":")
                if _norm_col(col) in low:
                    d.cols_appareil.append((header[low[_norm_col(col)]], typ, oeil))
                else:
                    d.cols_skezia_absentes.append(col)
            if _norm_col(COL_AF_OCT) in low:
                d.col_af_oct = header[low[_norm_col(COL_AF_OCT)]]
            else:
                d.cols_skezia_absentes.append(COL_AF_OCT)
        cols_extra = [c for c, _t, _o in d.cols_appareil] + ([d.col_af_oct] if d.col_af_oct else [])
        i_id = low[_norm_col(d.col_id)]
        i_centre = low[_norm_col(d.col_centre)] if d.col_centre else None
        i_sk = [(c, low[_norm_col(c)], t) for c, t, _o, _r in d.cols_skezia]
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            brut = norm_id_fredd(row[i_id] if i_id < len(row) else "")
            val = inv.extract_id(brut)
            if not val:
                d.lignes_sans_id += 1
                continue
            if not _FORMAT_ID_ATTENDU.fullmatch(brut):
                d.ids_format_inattendu += 1
            canon = inv.canonicalize_patient_id(val)
            centre = row[i_centre].strip() if i_centre is not None and i_centre < len(row) else ""
            skezia = {c: (row[i].strip() if i < len(row) else "") for c, i, _t in i_sk}
            extra = {c: (row[low[_norm_col(c)]].strip() if low[_norm_col(c)] < len(row) else "") for c in cols_extra}
            if canon in d.patients and d.patients[canon]["id"] != brut:
                d.ambigus[canon].update({d.patients[canon]["id"], brut})
                continue
            if canon in d.patients:
                # Même patient sur plusieurs lignes (ex. plusieurs visites) : une
                # image déclarée à N'IMPORTE QUELLE ligne compte comme déclarée.
                p = d.patients[canon]
                d.lignes_fusionnees += 1
                p["centre"] = p["centre"] or centre
                for c, v in skezia.items():
                    if valeur_declaree(v) is True or (valeur_declaree(p["skezia"][c]) is None and v):
                        p["skezia"][c] = v
                for c, v in extra.items():
                    if v and not p["extra"].get(c):
                        p["extra"][c] = v
                continue
            d.patients[canon] = {"id": brut, "centre": centre, "skezia": skezia, "extra": extra}
        return d


# Formats d'ID SKEZIA attendus : UUID complet, ou 8 hexadécimaux (forme tronquée).
_FORMAT_ID_ATTENDU = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?")
_UUID_COMPLET = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


FICHIER_ALIAS_CENTRES = Path(__file__).resolve().parent / "alias_centres.csv"


def charger_alias_centres(spec=None):
    """Correspondances « nom de centre rencontré -> nom retenu » (ex. HUS ->
    CARGO-HUS : même centre sous deux libellés). `spec` : chemin d'un CSV
    (nom_source;nom_retenu, lignes « # » ignorées), ou texte « A=B;C=D »,
    ou « aucun ». Par défaut : alias_centres.csv à côté de ce script, s'il
    existe. La comparaison ignore casse, accents, espaces et ponctuation."""
    if spec is None:
        spec = str(FICHIER_ALIAS_CENTRES) if FICHIER_ALIAS_CENTRES.is_file() else ""
    spec = str(spec).strip()
    paires = []
    if not spec or spec.lower() == "aucun":
        return {}, ""
    if Path(spec).is_file():
        with open(spec, encoding="utf-8-sig") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#"):
                    continue
                morceaux = [m.strip() for m in re.split(r"[;,\t]", ligne)]
                if len(morceaux) >= 2 and morceaux[0] and morceaux[1]:
                    if _norm_centre(morceaux[0]) in ("nomsource", "alias", "source"):
                        continue
                    paires.append((morceaux[0], morceaux[1]))
        origine = Path(spec).name
    else:
        for morceau in spec.split(";"):
            if "=" in morceau:
                a_, b_ = (x.strip() for x in morceau.split("=", 1))
                if a_ and b_:
                    paires.append((a_, b_))
        origine = "--alias-centres"
    return {_norm_centre(a_): b_ for a_, b_ in paires}, origine


def canon_centre(nom, alias):
    return alias.get(_norm_centre(nom), nom) if nom and alias else nom


def norm_id_fredd(v):
    """'  {300B72A1-C067-…} ' -> '300b72a1-c067-…' (casse, espaces, accolades,
    guillemets neutralisés pour que deux écritures du même ID soient égales)."""
    s = str(v or "").strip().strip('"\'').strip().strip("{}").strip()
    return s.lower() if re.fullmatch(r"[0-9A-Fa-f\-]+", s) else s


def uuids_sous_centre(chemin, racine):
    """UUID complets présents dans les noms situés sous le dossier-centre."""
    parts = parties_relatives(chemin, racine)[1:]
    return list(OrderedDict.fromkeys(m.group(0).lower() for p in parts for m in _UUID_COMPLET.finditer(p)))


def etape1_inventaire(dossiers, dossier_inv, opts, ids_connus):
    dossier_inv.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    inv_args = argparse.Namespace(
        extensions_image_suppl=opts.extensions_image_suppl,
        profondeur_zip_max=opts.profondeur_zip_max,
        taille_max_zip_memoire_mo=opts.taille_max_zip_memoire_mo,
        pas_de_zip=False, pas_de_hash=False,
    )
    inv_path = dossier_inv / f"inventaire_images_{ts}.csv"
    err_path = dossier_inv / f"erreurs_inventaire_{ts}.csv"
    with open(inv_path, "w", newline="", encoding="utf-8-sig") as f_inv, \
         open(err_path, "w", newline="", encoding="utf-8-sig") as f_err:
        w_inv = csv.writer(f_inv, delimiter=";")
        w_inv.writerow(inv.INVENTORY_HEADERS)
        w_err = csv.writer(f_err, delimiter=";")
        w_err.writerow(inv.ERROR_HEADERS)
        ctx = inv.Contexte(inv_args, w_inv, w_err, ids_connus)
        for dossier in dossiers:
            dpath = Path(dossier)
            if not dpath.exists():
                ctx.log_error(str(dpath), str(dpath), "(racine)", "dossier racine",
                              "DossierIntrouvable", "Le dossier indiqué n'existe pas.")
                continue
            inv.scan_root(dpath, ctx)
    inv.write_lot_report(dossier_inv / f"bilan_par_lot_{ts}.csv", ctx)
    _p, multi_rows = inv.write_multi_lot_report(dossier_inv / f"patients_multi_lots_{ts}.csv", ctx)
    inv.write_synthese_globale(dossier_inv / f"synthese_globale_{ts}.csv", ctx, multi_rows)

    def relire(p):
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))
    return relire(inv_path), relire(err_path), inv_path


_PRIORITE = {"A_TRAITER": 0, "NON_TRAITEE_FORMAT": 1, "MISE_DE_COTE_ID_AMBIGU": 2,
             "MISE_DE_COTE_SANS_PATIENT": 3, "MISE_DE_COTE_ID_NON_DETECTE": 4}


def etape2_tri(lignes_inv, donnees, garder_doublons=False, centre_depuis="dossier"):
    """Attribue à chaque image un statut provisoire et renvoie la liste des
    enregistrements. Les doublons (même SHA-256, même patient) sont résolus en
    gardant la copie la plus « utile » (à traiter > format non pris en charge
    > mise de côté)."""
    recs = []
    for ligne in lignes_inv:
        pid, obs_id, discordant = pseudo_sous_centre(ligne["chemin"], ligne["racine_scan"])
        inv_pid = ligne.get("patient_id_detecte") or ""
        if inv_pid and inv_pid != pid and not discordant:
            obs_id = (obs_id + " | " if obs_id else "") + \
                f"ID détecté par l'inventaire ('{inv_pid}') ignoré : hors convention ou hors du dossier-centre"
        r = {
            "chemin_source": ligne["chemin"], "racine_scan": ligne["racine_scan"], "lot": ligne["lot"],
            "centre": canon_centre(centre_depuis_lot(ligne["lot"]), getattr(donnees, "alias_centres", {})),
            "categorie": ligne["categorie"],
            "format_detecte": ligne["format_detecte"], "patient_id_detecte": pid,
            "sha256": ligne.get("sha256", ""), "id_fredd": "", "observation": obs_id,
            "nom_fichier": ligne.get("nom_fichier", ""),
        }
        if discordant:
            r["statut"] = "MISE_DE_COTE_ID_AMBIGU"
            # IDs en conflit (dossier ≠ fichier) : l'image n'est comptée pour
            # AUCUN patient à la confrontation, mais signalée chez chacun.
            r["_ids_discordants"] = list(OrderedDict.fromkeys(re.findall(r"[0-9a-f]{8}", obs_id)))
        elif not pid:
            r["statut"] = "MISE_DE_COTE_ID_NON_DETECTE"
        elif pid in donnees.ambigus:
            r["statut"] = "MISE_DE_COTE_ID_AMBIGU"
            r["observation"] = "IDs FREDD candidats : " + ", ".join(sorted(donnees.ambigus[pid]))
            r["_ids_discordants"] = [pid]   # compté pour personne, signalé
        elif pid not in donnees.patients:
            r["statut"] = "MISE_DE_COTE_SANS_PATIENT"
        else:
            id_csv = donnees.patients[pid]["id"]
            # Le préfixe suffit SAUF si le chemin porte un UUID complet : il doit
            # alors être exactement celui du CSV (sinon : autre patient au même
            # préfixe, ou faute de frappe -> on ne devine pas).
            complets = [u for u in uuids_sous_centre(ligne["chemin"], ligne["racine_scan"]) if u[:8] == pid]
            if complets and _UUID_COMPLET.fullmatch(id_csv) and any(u != id_csv for u in complets):
                r["statut"] = "MISE_DE_COTE_ID_AMBIGU"
                r["observation"] = (r["observation"] + " | " if r["observation"] else "") + \
                    f"UUID du chemin ({', '.join(complets)}) ≠ UUID FREDD ({id_csv}) malgré le même préfixe"
                r["_ids_discordants"] = [pid]   # compté pour personne, signalé
            else:
                r["id_fredd"] = id_csv
                r["statut"] = "A_TRAITER" if r["categorie"] in CATEGORIES_RARETIA else "NON_TRAITEE_FORMAT"
                if centre_depuis == "donnees":
                    centre_csv = (donnees.patients[pid].get("centre") or "").strip()
                    if centre_csv:
                        r["centre"] = centre_csv
                    else:
                        r["observation"] = (r["observation"] + " | " if r["observation"] else "") + \
                            f"centre absent des données FREDD : dossier d'origine utilisé ({r['centre']})"
        recs.append(r)

    # Contenu identique rattaché à des patients différents : incohérence à signaler
    pids_par_hash = defaultdict(set)
    for r in recs:
        if r["sha256"]:
            pids_par_hash[r["sha256"]].add(r["patient_id_detecte"] or "?")
    for r in recs:
        if r["sha256"] and len(pids_par_hash[r["sha256"]]) > 1:
            autres = sorted(pids_par_hash[r["sha256"]] - {r["patient_id_detecte"] or "?"})
            r["observation"] = (r["observation"] + " | " if r["observation"] else "") + \
                "Contenu binaire identique à une image rattachée à : " + ", ".join(autres)

    if not garder_doublons:
        groupes = defaultdict(list)
        for r in recs:
            if r["sha256"]:
                groupes[(r["sha256"], r["patient_id_detecte"])].append(r)
        for groupe in groupes.values():
            if len(groupe) < 2:
                continue
            groupe_trie = sorted(groupe, key=lambda x: _PRIORITE.get(x["statut"], 9))
            garde = groupe_trie[0]
            for r in groupe_trie[1:]:
                r["statut"] = "IGNOREE_DOUBLON"
                r["destination"] = ""
                r["motif"] = f"Identique à : {garde['chemin_source']}"
    return recs


def mettre_de_cote(recs, lecteur, sortie, simulation):
    """Copie les images non traitables dans les dossiers de mise à l'écart."""
    for r in recs:
        st = r["statut"]
        if st == "MISE_DE_COTE_SANS_PATIENT":
            base = sortie / "sans_patient" / safe_name(r["centre"])
        elif st == "MISE_DE_COTE_ID_NON_DETECTE":
            base = sortie / "sans_patient" / safe_name(r["centre"]) / "_id_non_detecte"
        elif st == "MISE_DE_COTE_ID_AMBIGU":
            base = sortie / "sans_patient" / safe_name(r["centre"]) / "_id_ambigu"
        elif st == "NON_TRAITEE_FORMAT":
            base = sortie / "non_traitees_format" / safe_name(r["centre"])
        elif st == "NON_TRAITEE_RAW_DATA":
            base = sortie / "non_traitees_format" / safe_name(r["centre"]) / "_raw_data"
        elif st == "NON_TRAITEE_DICOMDIR":
            base = sortie / "non_traitees_format" / safe_name(r["centre"]) / "_dicomdir"
        elif st == "NON_TRAITEE_ANGIO":
            base = sortie / "non_traitees_format" / safe_name(r["centre"]) / "_angiographie"
        elif st == "NON_TRAITEE_MONTAGE":
            base = sortie / "non_traitees_format" / safe_name(r["centre"]) / "_montage_reconstruit"
        elif st == "NON_TRAITEE_IR":
            base = sortie / "non_traitees_format" / safe_name(r["centre"]) / "_infrarouge"
        else:
            continue
        rel = parties_relatives(r["chemin_source"], r["racine_scan"])
        # le 1er composant est le centre lui-même : inutile de le répéter
        if len(rel) > 1:
            rel = rel[1:]
        dest = chemin_unique(base.joinpath(*rel))
        r["destination"] = str(dest.relative_to(sortie))
        if simulation:
            continue
        try:
            lecteur.copier_vers(r["chemin_source"], dest)
        except Exception as e:  # noqa: BLE001
            r["motif"] = f"Copie impossible : {type(e).__name__}: {e}"
            r["statut"] = "ECHEC_EXTRACTION"
            r["destination"] = ""


_TAGS_INTERDITS = ["PatientBirthDate", "PatientAddress", "PatientTelephoneNumbers", "OtherPatientIDs",
                   "OtherPatientNames", "PatientMotherBirthName", "MilitaryRank", "EthnicGroup",
                   "MedicalRecordLocator", "PatientBirthName"]


def identifiants_source(path):
    """Identifiants directs présents dans le DICOM source (pour vérifier qu'ils
    ne fuient pas dans la sortie)."""
    if pydicom is None or Path(path).suffix.lower() != ".dcm":
        return set()
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    except Exception:  # noqa: BLE001
        return set()
    vals = set()
    for kw in ("PatientID", "PatientName", "OtherPatientIDs", "PatientBirthName"):
        v = str(getattr(ds, kw, "") or "").strip()
        for morceau in [v] + re.split(r"[\^ ]+", v):
            if len(morceau) >= 4:
                vals.add(morceau)
    return vals


def orientation_coupe_dicom(path):
    """Orientation de la coupe OCT (« horizontale », « verticale »…) écrite par raretia dans
    ImageComments (« B-scan 3/49 ; orientation : horizontale »). Vide si absente (images en face,
    OCT issus d'un DICOM source, orientation non déterminée) ou si le fichier est illisible."""
    try:
        import pydicom
        txt = str(getattr(pydicom.dcmread(str(path), stop_before_pixels=True, force=True), "ImageComments", "") or "")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"orientation\s*:\s*([^;]+)", txt)
    return m.group(1).strip() if m else ""


def champ_dicom(path):
    """Champ de l'image (« grand_champ » / « maculaire ») écrit par raretia dans ImageComments
    (« champ : grand_champ (méthode : coins) ») pour l'AF et le fond d'œil couleur, d'après la règle
    des 4 coins du module. Vide si absent ou si le fichier est illisible."""
    try:
        import pydicom
        txt = str(getattr(pydicom.dcmread(str(path), stop_before_pixels=True, force=True), "ImageComments", "") or "")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"champ\s*:\s*([^;(]+)", txt)
    return m.group(1).strip() if m else ""


def texte_incruste_declare(path):
    """True si le DICOM source déclare du texte incrusté dans l'image (BurnedInAnnotation = YES).
    Le pipeline ne lit PAS les pixels : seule cette déclaration de l'appareil est exploitée."""
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        return str(getattr(ds, "BurnedInAnnotation", "") or "").strip().upper() == "YES"
    except Exception:  # noqa: BLE001
        return False


def controler_pseudonymisation(path, patientid_attendu, ids_source):
    """Retourne (ok, motif). Contrôle :
      - PatientName vide ;
      - PatientID == pseudonyme attendu ;
      - aucun tag d'identification directe renseigné ;
      - aucun identifiant du DICOM source présent dans un élément texte."""
    if pydicom is None:
        return None, "pydicom absent : contrôle non effectué"
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    except Exception as e:  # noqa: BLE001
        return False, f"Sortie illisible : {type(e).__name__}: {e}"
    pb = []
    if str(getattr(ds, "PatientName", "") or "").strip():
        pb.append("PatientName non vide")
    pid = str(getattr(ds, "PatientID", "") or "").strip()
    if pid != patientid_attendu:
        pb.append(f"PatientID inattendu ('{pid}' au lieu de '{patientid_attendu}')")
    for kw in _TAGS_INTERDITS:
        if str(getattr(ds, kw, "") or "").strip():
            pb.append(f"{kw} renseigné")
    ids = {i for i in ids_source if i and i not in patientid_attendu}
    if ids:
        for el in ds.iterall():
            if el.VR in ("UI", "OB", "OW", "SQ", "UN") or el.keyword == "PatientID":
                continue
            try:
                txt = str(el.value)
            except Exception:  # noqa: BLE001
                continue
            fuite = next((i for i in ids if i in txt), None)
            if fuite:
                pb.append(f"identifiant source retrouvé dans {el.keyword or el.tag}")
                break
    return (not pb), "; ".join(pb)


def harmoniser_patientid(path, id_fredd):
    """Remplace le PatientID technique (<id>__<empreinte>) par l'identifiant
    FREDD, pour que toutes les images d'un patient partagent le même
    PatientID (utile notamment au recalage maculaire <-> grand champ)."""
    ds = pydicom.dcmread(str(path), force=True)
    ds.PatientID = id_fredd
    ds.save_as(str(path))


SEUIL_PIXEL_UTILE = 15


def mesures_image_dicom(path):
    """(largeur, hauteur, % de pixels « utiles ») d'un DICOM écrit. Un pixel est utile si son niveau de
    gris dépasse 15 (le seuil de la zone rétinienne du module pipeline_wp2) : une image presque noire donne
    un pourcentage proche de 0. Mesures seulement, aucun seuil appliqué ici ; chaînes vides si illisible."""
    try:
        import numpy as np
        ds = pydicom.dcmread(str(path), force=True)
        px = np.asarray(ds.pixel_array)
        if px.ndim == 4 or (px.ndim == 3 and int(getattr(ds, "SamplesPerPixel", 1) or 1) == 1):
            px = px[0]                                  # multi-image : première
        gris = px.mean(axis=-1) if px.ndim == 3 else px
        return int(ds.Columns), int(ds.Rows), f"{100.0 * float((gris > SEUIL_PIXEL_UTILE).mean()):.1f}"
    except Exception:  # noqa: BLE001
        return "", "", ""


def commentaire_qualite(accepte, score, problemes):
    """Texte ASCII de l'évaluation technique automatique, ajouté à ImageComments :
    « QUALITE_TECHNIQUE=OK|LIMITEE|INCONNU ; score=0.82 ; problemes=… ». Ce n'est pas une
    évaluation clinique. ASCII seulement (jeu de caractères DICOM par défaut) ; pas de « ; » dans
    les problèmes, pour que le champ reste lisible par découpage."""
    drapeau = {True: "OK", False: "LIMITEE", None: "INCONNU"}[accepte]
    morceaux = [f"QUALITE_TECHNIQUE={drapeau}"]
    try:
        morceaux.append(f"score={float(str(score).replace(',', '.')):.2f}")
    except ValueError:
        pass
    texte = unicodedata.normalize("NFKD", str(problemes or "")).encode("ascii", "ignore").decode("ascii")
    texte = re.sub(r"\s+", " ", texte.replace(";", ",")).strip()[:300]
    if texte:
        morceaux.append("problemes=" + texte)
    return " ; ".join(morceaux)


def annoter_qualite_dicom(path, accepte, score, problemes):
    """Ajoute l'évaluation technique à ImageComments (après l'éventuel « B-scan 3/49 ; orientation : … »,
    qui reste lisible). Idempotent : une évaluation déjà présente est remplacée. True si écrit."""
    try:
        ds = pydicom.dcmread(str(path), force=True)
        existant = re.sub(r"\s*;?\s*QUALITE_TECHNIQUE=.*$", "", str(getattr(ds, "ImageComments", "") or "")).strip()
        ds.ImageComments = ((existant + " ; ") if existant else "") + commentaire_qualite(accepte, score, problemes)
        ds.save_as(str(path))
        return True
    except Exception:  # noqa: BLE001
        return False


def _statut_depuis_sorties(sorties):
    """Statut d'un fichier source d'après ses sorties. Le contrôle de pseudonymisation
    est fait image par image : un fichier source peut n'avoir qu'une partie de ses
    sorties en quarantaine — cas fréquent pour un volume OCT dont un seul B-scan porte
    le bandeau d'identification. Ce cas a son propre statut, pour que le journal ne
    déclare pas « quarantaine » un fichier dont la plupart des images sont livrées."""
    if not sorties:
        return "ECHEC_TRAITEMENT"
    echecs = [s.get("pseudonymisation") == "ECHEC" for s in sorties]
    if all(echecs):
        return "ECHEC_PSEUDONYMISATION"
    if any(echecs):
        return "QUARANTAINE_PARTIELLE"
    acc = [s["acceptee_bool"] for s in sorties]
    if any(a is False for a in acc):
        return "TRAITEE_QUALITE_LIMITEE"
    if acc and all(a is True for a in acc):
        return "TRAITEE_OK"
    return "TRAITEE_QUALITE_INCONNUE"


CONTENU_HEADERS = ["chemin_source", "centre", "patient_id_detecte", "id_fredd", "statut_tri", "categorie",
                   "serie", "modalite", "lateralite", "source_oeil", "orientation", "nb_bscans", "motif_scan",
                   "appareil", "alerte", "detail", "erreur"]


def resume_contenu(lignes):
    morceaux = []
    for c in lignes:
        if c.get("erreur"):
            morceaux.append(f"? ({c['erreur']})")
            continue
        m = " ".join(x for x in (c["modalite"], c["lateralite"], c["orientation"]) if x)
        if c["nb_bscans"]:
            m += f" ×{c['nb_bscans']}"
        if c.get("alerte"):
            m += f" ⚠ {c['alerte']}"
        morceaux.append(m)
    return " ; ".join(morceaux)


def etape1b_contenu(recs, lecteur, sortie, opts):
    """Analyse le contenu de chaque image (sauf doublons) sans décoder les
    pixels. Les fichiers situés dans des zips sont extraits temporairement."""
    lignes_csv = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="fredd_contenu_",
                                    dir=opts.dossier_travail if opts.dossier_travail else None))
    try:
        cibles = [r for r in recs if r["statut"] != "IGNOREE_DOUBLON"]
        for i, r in enumerate(cibles, 1):
            if i % 500 == 0:
                print(f"  ... contenu analysé : {i}/{len(cibles)}")
            chemin = r["chemin_source"]
            fichier, temporaire = None, None
            try:
                if r["categorie"] in ("DICOM", "E2E_HEIDELBERG"):
                    if " :: " in chemin:
                        ext = ".e2e" if r["categorie"] == "E2E_HEIDELBERG" else ".dcm"
                        temporaire = lecteur.copier_vers(chemin, tmp_dir / f"f{i}{ext}")
                        fichier = temporaire
                    else:
                        fichier = inv.long_path_str(chemin)
                contenu = ac.analyser(fichier, r["categorie"], chemin)
            except Exception as e:  # noqa: BLE001
                contenu = [ac.ligne(erreur=f"{type(e).__name__}: {e}")]
            finally:
                if temporaire is not None:
                    try:
                        os.remove(temporaire)
                    except OSError:
                        pass
            r["_contenu"] = contenu
            r["contenu_detecte"] = resume_contenu(contenu)
            for c in contenu:
                lignes_csv.append({**c, "chemin_source": chemin, "centre": r["centre"],
                                   "patient_id_detecte": r["patient_id_detecte"], "id_fredd": r["id_fredd"],
                                   "statut_tri": r["statut"], "categorie": r["categorie"]})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    ecrire_csv(sortie / "01_inventaire" / "contenu_images.csv", CONTENU_HEADERS, lignes_csv)
    return lignes_csv


MOTIF_IR = "E2E uniquement infrarouge : IR exclues par décision FREDD (15/10/2026)"


def _que_de_l_ir(contenu):
    utiles = [c for c in contenu if not c.get("erreur")]
    return bool(utiles) and all(c.get("modalite") == "infrarouge" for c in utiles)


def _que_angio(contenu):
    """Tout le contenu analysable est hors périmètre et au moins une série est une angiographie (FA / ICGA)."""
    utiles = [c for c in contenu if not c.get("erreur")]
    return (bool(utiles) and any(c.get("modalite") in ac.MODALITES_ANGIO for c in utiles)
            and all(c.get("modalite") in ac.MODALITES_ANGIO + ("infrarouge", "raw_data") for c in utiles))


def _famille(t):
    t = t or ""
    return "oct_bscan" if t.startswith("oct") else "autofluorescence" if t.startswith("autofluo") else "fond"


def series_manquantes(contenu, types_sortis):
    """Familles d'images (OCT, AF, fond/en face) détectées à l'inventaire mais
    absentes des sorties raretia. Filet de sécurité : sans lui, un OCT que le
    lecteur n'arrive pas à décoder (ex. date d'acquisition nulle sous Windows)
    disparaîtrait sans bruit, le fichier restant « traité » grâce à ses autres
    images. Les IR (exclues par décision FREDD) et les Raw Data sont ignorés."""
    attendues = {_famille(c.get("modalite")) for c in contenu or []
                 if not c.get("erreur") and c.get("modalite") not in ("infrarouge", "raw_data", "inconnu", "image_standard", "", *ac.MODALITES_ANGIO)}
    sorties = {_famille(t) for t in types_sortis}
    return sorted(attendues - sorties)


def marquer_raw_data(recs):
    """Fichiers dont rien ne sortirait de raretia, à ne pas lui envoyer. Statut
    dédié plutôt qu'ECHEC_TRAITEMENT, qui laisserait croire à un dysfonctionnement :
      - DICOM entièrement « Raw Data » (raretia les ignore, aucune image) ;
      - E2E ne contenant que de l'infrarouge (IR exclues par décision FREDD,
        voir pipeline_wp2/reader.py)."""
    for r in recs:
        cont = r.get("_contenu") or []
        if r["statut"] != "A_TRAITER" or not cont:
            continue
        if r["categorie"] == "DICOM" and all(c.get("modalite") == "dicomdir" for c in cont):
            r["statut"] = "NON_TRAITEE_DICOMDIR"
            r["motif"] = "DICOMDIR (SOP Class 1.2.840.10008.1.3.10) : index de support, pas d'image"
        elif r["categorie"] == "DICOM" and all(c.get("modalite") == "raw_data" for c in cont):
            r["statut"] = "NON_TRAITEE_RAW_DATA"
            r["motif"] = "SOP Class Raw Data Storage (1.2.840.10008.5.1.4.1.1.66) : pas d'image"
        elif r["categorie"] in ("DICOM", "E2E_HEIDELBERG") and _que_angio(cont):
            r["statut"] = "NON_TRAITEE_ANGIO"
            r["motif"] = "Angiographie (FA / ICGA) : hors périmètre FREDD (décision du 22/10/2026)"
        elif r["categorie"] == "E2E_HEIDELBERG" and _que_de_l_ir(cont):
            r["statut"] = "NON_TRAITEE_IR"
            r["motif"] = MOTIF_IR


def marquer_montages(recs):
    """Décision FREDD : on garde les images NATIVES, pas les montages reconstruits.

    Un montage (voir analyse_contenu.montage_dicom) n'est écarté (NON_TRAITEE_MONTAGE) que si TOUTES ses
    images sources sont présentes pour le même patient et encore à traiter : sinon on le garde, pour ne
    perdre aucune surface rétinienne, et le motif le dit. Les natives servant à un montage sont repérées
    (`composante_montage` dans le détail du contenu) ; elles sont conservées. Sans analyse du contenu
    (--pas-analyse-contenu), rien n'est écarté. Les UID ne sont jamais écrits dans les images produites."""
    par_uid = {}
    for r in recs:
        if r["statut"] not in ("A_TRAITER", "IGNOREE_DOUBLON", "A_TRAITER_SIMULATION"):
            continue
        for c in r.get("_contenu") or []:
            if c.get("sop_uid"):
                par_uid.setdefault((r["id_fredd"], c["sop_uid"]), []).append((r, c))
    for r in recs:
        cont = r.get("_contenu") or []
        if r["statut"] != "A_TRAITER" or not cont or not r["id_fredd"]:
            continue
        if not all(c.get("montage") for c in cont) or r["categorie"] != "DICOM":
            continue
        sources = [u for c in cont for u in c.get("sources_montage") or []]
        manquantes = [u for u in sources if (r["id_fredd"], u) not in par_uid]
        if not sources or manquantes:
            for c in cont:
                c["alerte"] = (c["alerte"] + " | " if c.get("alerte") else "") + (
                    "montage reconstruit conservé : image(s) source absente(s)" if sources else
                    "montage reconstruit conservé : sources non citées")
            continue
        r["statut"] = "NON_TRAITEE_MONTAGE"
        r["motif"] = (f"Montage reconstruit (ImageType MONTAGE) : ses {len(sources)} image(s) native(s) sont "
                      "présentes et traitées ; seules les natives sont conservées (décision FREDD 22/10/2026)")
        for u in sources:
            for rs, c in par_uid[(r["id_fredd"], u)]:
                if "composante_montage" not in c["detail"]:
                    c["detail"] += "; composante_montage"
                    rs["contenu_detecte"] = (rs.get("contenu_detecte") or "") + " [composante d'un montage]"


def _est_dicomdir(path):
    if pydicom is None:
        return False
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        media = str(getattr(getattr(ds, "file_meta", None), "MediaStorageSOPClassUID", "") or "")
        return ac.SOP_DICOMDIR in (str(getattr(ds, "SOPClassUID", "")), media)
    except Exception:  # noqa: BLE001
        return False


def _est_raw_data(path):
    if pydicom is None:
        return False
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        return str(getattr(ds, "SOPClassUID", "")) == ac.SOP_RAW_DATA
    except Exception:  # noqa: BLE001
        return False


def _duree(s):
    s = int(round(s))
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else (f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s")


class SuiviProgression:
    """Affiche l'avancement du module pipeline_wp2 pendant un paquet : fichier en
    cours, pourcentage, temps écoulé et estimation du temps restant. Sur une
    console, la ligne est réécrite sur place ; sinon (journal redirigé), une
    ligne tous les 10 %. Les noms de fichiers ne sont jamais affichés."""

    def __init__(self, libelle):
        self.libelle = libelle
        self.t0 = time.time()
        self.tty = sys.stdout.isatty()
        self.dernier_palier = -1
        self.ecrit = False

    def __call__(self, i, n):
        if n <= 0:
            return
        fait = i  # i = index (0-based) du fichier qui commence : i fichiers terminés
        ecoule = time.time() - self.t0
        reste = f" · reste ≈ {_duree(ecoule / fait * (n - fait))}" if fait else ""
        ligne = (f"    {self.libelle} : fichier {i + 1}/{n} ({100 * fait // n} %) · "
                 f"{_duree(ecoule)} écoulées{reste}")
        if self.tty:
            print("\r" + ligne.ljust(110), end="", flush=True)
            self.ecrit = True
        else:
            palier = 10 * fait // n
            if palier != self.dernier_palier:
                self.dernier_palier = palier
                print(ligne, flush=True)

    def terminer(self):
        if self.ecrit:
            print()


def etape3_traitement(recs, lecteur, sortie, opts, paquets_log, checkpoint=None):
    """checkpoint(sorties_rows) : appelé après CHAQUE paquet rangé, pour écrire l'état de reprise
    (rapports/etat_*.csv) ; sans cela, une coupure en cours de route ferait tout retraiter."""
    a_traiter = [r for r in recs if r["statut"] == "A_TRAITER"]
    if opts.simulation:
        for r in a_traiter:
            r["statut"] = "A_TRAITER_SIMULATION"
            r["destination"] = str(Path("images_traitees") / safe_name(r["centre"]) / opts.date_traitement
                                    / safe_name(r["id_fredd"]))
        return []

    travail = Path(opts.dossier_travail) if opts.dossier_travail else sortie / "_travail"
    sorties_rows = []
    par_centre = defaultdict(list)
    for r in a_traiter:
        par_centre[r["centre"]].append(r)

    extra = opts.raretia_args.split() if opts.raretia_args else None
    total = len(a_traiter)
    deja = 0
    t_etape = time.time()
    # Dossiers de travail à noms COURTS : le module pipeline_wp2 n'utilise pas le préfixe
    # Windows \\?\ et recopie le nom du dossier d'entrée dans son chemin de sortie
    # (<sortie>/<nom>/processed/<id>__<empreinte>/...). Avec un nom de centre long et
    # un --sortie profond, on dépasserait les 260 caractères de Windows.
    n_appel = 0
    for centre, liste in sorted(par_centre.items()):
        centre_s = safe_name(centre)
        nb_paquets = -(-len(liste) // opts.taille_paquet)
        print(f"\n=== Étape 3 — centre {centre} : {len(liste)} image(s) à traiter "
              f"({nb_paquets} paquet(s) de {opts.taille_paquet} max.) ===")
        for n_paquet, debut in enumerate(range(0, len(liste), opts.taille_paquet), start=1):
            paquet = liste[debut: debut + opts.taille_paquet]
            nom = f"{centre_s}__paquet_{n_paquet:03d}"
            n_appel += 1
            stage = travail / "s" / f"{n_appel:05d}"
            brut = travail / "b" / f"{n_appel:05d}"
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(brut, ignore_errors=True)
            stage.mkdir(parents=True, exist_ok=True)

            # 3a. Staging : copie sous un nom NEUTRE <id_fredd>__<empreinte>.<ext>
            #     -> raretia mettra ce nom dans PatientID (jamais le nom d'origine,
            #        qui peut contenir des informations identifiantes).
            stems = {}
            for i, r in enumerate(paquet):
                ext = CATEGORIES_RARETIA[r["categorie"]]
                stem = f"{safe_name(r['id_fredd'], 60)}__{(r['sha256'] or f'n{debut + i:06d}')[:12]}"
                while stem in stems:
                    stem += "b"
                try:
                    dest = lecteur.copier_vers(r["chemin_source"], stage / (stem + ext))
                except Exception as e:  # noqa: BLE001
                    r["statut"] = "ECHEC_EXTRACTION"
                    r["motif"] = f"{type(e).__name__}: {e}"
                    continue
                r["_stem"], r["_stage"] = stem, dest
                r["_ids_source"] = identifiants_source(dest)
                r["_texte_incruste"] = bool(pydicom) and r["categorie"] == "DICOM" and texte_incruste_declare(dest)
                stems[stem] = r
            if not stems:
                continue

            # 3b. Appel raretia sur tout le paquet
            print(f"  [{nom}] {len(stems)} fichier(s) envoyé(s) au module pipeline_wp2 "
                  f"(paquet {n_paquet}/{nb_paquets} du centre ; {deja}/{total} faits au total, "
                  f"étape 3 en cours depuis {_duree(time.time() - t_etape)})", flush=True)
            suivi = SuiviProgression(f"paquet {n_paquet}/{nb_paquets}")
            res = ra.run_raretia(stage, brut, set(stems), sortie / "rapports" / "journal_raretia" / f"{nom}.log",
                                 raretia_path=opts.raretia_path, module=opts.raretia_module,
                                 extra_args=extra, timeout_s=opts.timeout_paquet, progression=suivi)
            suivi.terminer()
            deja += len(paquet)
            paquets_log.append({"paquet": nom, "centre": centre, "nb_images": len(stems),
                                "code_retour": res.returncode, "duree_s": round(res.duree_s, 1),
                                "message": res.message, "log": str(res.log_path.relative_to(sortie)),
                                "reessai_unitaire": ""})
            print(f"  [{nom}] raretia : code {res.returncode}, {sum(map(len, res.sorties.values()))} "
                  f"sortie(s) en {res.duree_s:.1f} s")

            # 3c. Réessai image par image si le paquet a planté (isole la/les image(s) fautive(s))
            manquants = [s for s in stems if s not in res.sorties]
            # Le code de retour de raretia vaut 0 même quand des fichiers ne donnent aucune image (le lecteur
            # journalise l'erreur et continue) : on réessaie donc aussi à code 0, sauf pour les fichiers dont
            # l'absence de sortie est attendue (DICOMDIR, Raw Data, E2E uniquement IR).
            if manquants and res.returncode == 0:
                manquants = [s for s in manquants if not (_est_dicomdir(stems[s]["_stage"])
                                                           or _est_raw_data(stems[s]["_stage"])
                                                           or (stems[s]["categorie"] == "E2E_HEIDELBERG"
                                                               and _que_de_l_ir(stems[s].get("_contenu") or [])))]
            if manquants and not opts.pas_de_reessai_unitaire:
                print(f"  [{nom}] " + ("échec global" if res.returncode != 0 else "image(s) sans sortie malgré un code 0")
                      + f" : réessai unitaire de {len(manquants)} image(s)")
                paquets_log[-1]["reessai_unitaire"] = f"{len(manquants)} image(s)"
                for s in manquants:
                    r = stems[s]
                    n_appel += 1
                    st1 = travail / "s" / f"{n_appel:05d}"
                    br1 = travail / "b" / f"{n_appel:05d}"
                    shutil.rmtree(st1, ignore_errors=True)
                    shutil.rmtree(br1, ignore_errors=True)
                    st1.mkdir(parents=True)
                    shutil.copy2(r["_stage"], st1 / r["_stage"].name)
                    res1 = ra.run_raretia(st1, br1, {s}, sortie / "rapports" / "journal_raretia" / "unitaire" / f"{s}.log",
                                          raretia_path=opts.raretia_path, module=opts.raretia_module,
                                          extra_args=extra, timeout_s=opts.timeout_paquet)
                    if s in res1.sorties:
                        res.sorties[s] = res1.sorties[s]
                    else:
                        r["motif"] = f"raretia (unitaire) : {res1.message or 'aucune sortie'}"
                        r["_log"] = str(res1.log_path.relative_to(sortie))

            # 3d. Contrôle pseudonymisation + rangement par centre / patient
            for s, r in stems.items():
                lignes = []
                for so in res.sorties.get(s, []):
                    note_qualite = ""
                    ok, motif_pseudo = controler_pseudonymisation(so.chemin, s, r["_ids_source"])
                    if r.get("_texte_incruste") and ok is not False:
                        ok = False
                        motif_pseudo = ((motif_pseudo + "; ") if motif_pseudo else "") + \
                            "texte incrusté déclaré dans l'image source (BurnedInAnnotation = YES)"
                    if ok is not False and opts.controle_texte:
                        texte_ok, motif_texte = ct.controler_sortie(so.chemin)
                        if not texte_ok:
                            ok = False
                            motif_pseudo = ((motif_pseudo + "; ") if motif_pseudo else "") + motif_texte
                    if ok is False:
                        base = sortie / "quarantaine_pseudonymisation" / centre_s / safe_name(r["id_fredd"])
                        etat = "ECHEC"
                    else:
                        base = sortie / "images_traitees" / centre_s / opts.date_traitement / safe_name(r["id_fredd"])
                        etat = "OK" if ok else "NON_VERIFIE"
                        attendu = s
                        if ok and not opts.garder_patientid_raretia:
                            harmoniser_patientid(so.chemin, r["id_fredd"])
                            attendu = r["id_fredd"]  # le PatientID vient d'être remplacé
                        if not annoter_qualite_dicom(so.chemin, so.accepted, so.score, so.issues):
                            note_qualite = "évaluation qualité non écrite dans ImageComments"
                        # Les deux écritures ci-dessus (PatientID, ImageComments) ont lieu APRÈS
                        # le contrôle de pseudonymisation : le fichier livré n'était donc pas celui qui
                        # avait été validé. On revalide l'en-tête après la dernière écriture, pour que la
                        # garantie porte sur le fichier effectivement rangé dans images_traitees/.
                        if ok:
                            ok2, motif2 = controler_pseudonymisation(so.chemin, attendu, r["_ids_source"])
                            if ok2 is False:
                                ok = False
                                base = sortie / "quarantaine_pseudonymisation" / centre_s / safe_name(r["id_fredd"])
                                etat = "ECHEC"
                                motif_pseudo = ((motif_pseudo + "; ") if motif_pseudo else "") + \
                                    f"après écriture finale : {motif2}"
                    dest = chemin_unique(base / so.chemin.name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(so.chemin), str(dest))
                    largeur, hauteur, pct_utiles = mesures_image_dicom(dest)
                    lignes.append({
                        "centre": centre, "id_fredd": r["id_fredd"],
                        "fichier_sortie": str(dest.relative_to(sortie)), "chemin_source": r["chemin_source"],
                        "image_type": so.image_type, "lateralite": so.laterality,
                        "orientation_coupe": orientation_coupe_dicom(dest) if so.image_type == "oct_bscan" else "",
                        "champ": champ_dicom(dest) if so.image_type != "oct_bscan" else "",
                        "largeur_px": largeur, "hauteur_px": hauteur, "pct_pixels_utiles": pct_utiles,
                        "score_qualite": so.score,
                        "acceptee": {True: "OUI", False: "QUALITÉ LIMITÉE", None: "INCONNU"}[so.accepted],
                        "acceptee_bool": so.accepted, "problemes": so.issues,
                        "motif": " | ".join(x for x in (so.reason, note_qualite) if x),
                        "pseudonymisation": etat + (f" ({motif_pseudo})" if motif_pseudo and etat != "OK" else ""),
                        "rangement": "quarantaine" if etat == "ECHEC" else "images_traitees",
                    })
                for l in lignes:
                    l["pseudonymisation_etat"] = l["pseudonymisation"].split(" ")[0]
                r["statut"] = _statut_depuis_sorties(
                    [{"pseudonymisation": l["pseudonymisation_etat"], "acceptee_bool": l["acceptee_bool"]} for l in lignes])
                manque = series_manquantes(r.get("_contenu"), [l["image_type"] for l in lignes]) if lignes else []
                if manque and r["statut"] in ("TRAITEE_OK", "TRAITEE_QUALITE_INCONNUE", "TRAITEE_QUALITE_LIMITEE",
                                              "QUARANTAINE_PARTIELLE"):
                    # La quarantaine partielle prime sur « qualité limitée » : elle ne doit pas être
                    # masquée, sinon le journal perd la trace des images écartées.
                    if r["statut"] != "QUARANTAINE_PARTIELLE":
                        r["statut"] = "TRAITEE_QUALITE_LIMITEE"
                    r["_manque"] = "séries détectées à l'inventaire mais absentes des sorties raretia : " + ", ".join(manque)
                r["nb_sorties"] = len(lignes)
                r["nb_sorties_quarantaine"] = sum(1 for l in lignes if l["rangement"] == "quarantaine")
                r["nb_sorties_livrees"] = len(lignes) - r["nb_sorties_quarantaine"]
                r["types_sorties"] = ", ".join(sorted({l["image_type"] for l in lignes if l["image_type"]}))
                scores = []
                for l in lignes:
                    try:
                        scores.append(float(str(l["score_qualite"]).replace(",", ".")))
                    except ValueError:
                        pass
                r["score_min"] = f"{min(scores):.3f}" if scores else ""
                if r["statut"] == "TRAITEE_QUALITE_LIMITEE":
                    r["motif"] = " | ".join(sorted({l["motif"] for l in lignes if l["acceptee_bool"] is False and l["motif"]}
                                                   | ({r["_manque"]} if r.get("_manque") else set())))
                if r["statut"] in ("ECHEC_PSEUDONYMISATION", "QUARANTAINE_PARTIELLE"):
                    motifs = [l["pseudonymisation"] for l in lignes if l["pseudonymisation_etat"] == "ECHEC"]
                    if r["statut"] == "QUARANTAINE_PARTIELLE":
                        motifs.insert(0, f"{r['nb_sorties_quarantaine']}/{len(lignes)} sortie(s) en quarantaine, "
                                         f"{r['nb_sorties_livrees']} livrée(s)")
                    if r.get("_manque"):
                        motifs.append(r["_manque"])
                    r["motif"] = " | ".join(motifs)
                    quarantaine = str(Path("quarantaine_pseudonymisation") / centre_s / safe_name(r["id_fredd"]))
                    livrees = str(Path("images_traitees") / centre_s / opts.date_traitement / safe_name(r["id_fredd"]))
                    r["destination"] = quarantaine if r["statut"] == "ECHEC_PSEUDONYMISATION" \
                        else f"{quarantaine} + {livrees}"
                elif lignes:
                    r["destination"] = str(Path("images_traitees") / centre_s / opts.date_traitement / safe_name(r["id_fredd"]))
                if (r["statut"] == "ECHEC_TRAITEMENT" and r["categorie"] == "E2E_HEIDELBERG"
                        and _que_de_l_ir(r.get("_contenu") or ac.analyser_e2e(r["_stage"]))):
                    r["statut"] = "NON_TRAITEE_IR"
                    r["motif"] = MOTIF_IR
                    dest = chemin_unique(sortie / "non_traitees_format" / centre_s / "_infrarouge"
                                         / safe_name(r["id_fredd"]) / r["_stage"].name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(r["_stage"], dest)
                    r["destination"] = str(dest.relative_to(sortie))
                if r["statut"] == "ECHEC_TRAITEMENT" and _est_dicomdir(r["_stage"]):
                    # Filet si l'analyse de contenu était désactivée (--pas-analyse-contenu).
                    r["statut"] = "NON_TRAITEE_DICOMDIR"
                    r["motif"] = "DICOMDIR (SOP Class 1.2.840.10008.1.3.10) : index de support, pas d'image"
                    dest = chemin_unique(sortie / "non_traitees_format" / centre_s / "_dicomdir"
                                         / safe_name(r["id_fredd"]) / r["_stage"].name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(r["_stage"], dest)
                    r["destination"] = str(dest.relative_to(sortie))
                if r["statut"] == "ECHEC_TRAITEMENT" and _est_raw_data(r["_stage"]):
                    r["statut"] = "NON_TRAITEE_RAW_DATA"
                    r["motif"] = "SOP Class Raw Data Storage (1.2.840.10008.5.1.4.1.1.66) : pas d'image"
                    dest = chemin_unique(sortie / "non_traitees_format" / centre_s / "_raw_data"
                                         / safe_name(r["id_fredd"]) / r["_stage"].name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(r["_stage"], dest)
                    r["destination"] = str(dest.relative_to(sortie))
                if r["statut"] == "ECHEC_TRAITEMENT":
                    # Nom NEUTRE (r["_stage"].name, déjà <id_fredd>__<empreinte>.ext) -- jamais
                    # r["nom_fichier"] (nom d'origine, qui peut être identifiant).
                    dest = chemin_unique(sortie / "echecs_traitement" / centre_s / safe_name(r["id_fredd"])
                                         / r["_stage"].name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(r["_stage"], dest)
                    r["destination"] = str(dest.relative_to(sortie))
                    if not r.get("motif"):
                        r["motif"] = f"raretia : {res.message or 'aucune sortie produite pour cette image'}"
                sorties_rows.extend(lignes)

            # 3e. Copie des rapports bruts raretia (traçabilité), nettoyage du travail
            for rp in res.rapports_trouves:
                d = sortie / "rapports" / "raretia_bruts" / nom
                d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rp, d / rp.name)
            if not opts.conserver_travail:
                shutil.rmtree(stage, ignore_errors=True)
                shutil.rmtree(brut, ignore_errors=True)
            if checkpoint:
                checkpoint(sorties_rows)   # le paquet est rangé : on le mémorise tout de suite
    if not opts.conserver_travail:
        shutil.rmtree(travail, ignore_errors=True)
    return sorties_rows


def _norm_centre(s):
    """Forme comparable d'un nom de centre. Un numéro de centre écrit « 2.0 » ou « 2,0 » (colonne
    numérique relue comme un décimal) vaut « 2 »."""
    s = str(s if s is not None else "")
    if re.fullmatch(r"\s*\d+(?:[.,]0+)?\s*", s):
        s = str(int(float(s.replace(",", "."))))
    return re.sub(r"[^a-z0-9]", "", ascii_lower(s))


def etape5_confrontation(recs, sorties_rows, donnees):
    par_pid = defaultdict(list)
    discordantes = defaultdict(list)  # id -> images où cet ID est en conflit avec un autre
    for r in recs:
        if r.get("_ids_discordants"):
            for i in r["_ids_discordants"]:
                discordantes[i].append(r)
        elif r["patient_id_detecte"]:
            par_pid[r["patient_id_detecte"]].append(r)
    avec_raretia = bool(sorties_rows)
    contenu_analyse = any("_contenu" in r for r in recs)
    types_par_id = defaultdict(Counter)       # id -> Counter(type)
    types_oeil_par_id = defaultdict(Counter)  # id -> Counter((type, oeil))
    champ_par_id = defaultdict(Counter)       # id -> Counter((type, oeil, champ)) : AF maculaire / grand champ
    for s in sorties_rows:
        if s["rangement"] == "images_traitees":
            types_par_id[s["id_fredd"]][s["image_type"] or "?"] += 1
            types_oeil_par_id[s["id_fredd"]][(s["image_type"] or "?", norm_oeil(s["lateralite"]))] += 1
            champ_par_id[s["id_fredd"]][(s["image_type"] or "?", norm_oeil(s["lateralite"]), s.get("champ") or "")] += 1

    lignes = []
    tous = set(donnees.patients) | set(par_pid)
    for canon in sorted(tous):
        p = donnees.patients.get(canon)
        imgs = [r for r in par_pid.get(canon, []) if r["statut"] != "IGNOREE_DOUBLON"]
        nb_inv = len(imgs)
        # Présence d'images : un « Raw Data » n'est pas une image (aucun pixel affichable).
        nb_images_reelles = sum(1 for r in imgs if r["statut"] not in ("NON_TRAITEE_RAW_DATA", "NON_TRAITEE_DICOMDIR"))  # une IR reste une image
        centres = sorted({r["centre"] for r in imgs})
        l = {
            "id_fredd": p["id"] if p else canon,
            "present_donnees_fredd": "Oui" if p else "Non",
            "centre_donnees": p["centre"] if p else "",
            "centres_images": ", ".join(centres),
            "nb_images_inventoriees": nb_inv,
            "nb_images_traitees": sum(r["statut"] in STATUTS_TRAITES for r in imgs),
            "nb_qualite_limitee": sum(r["statut"] == "TRAITEE_QUALITE_LIMITEE" for r in imgs),
            "nb_echecs": sum(r["statut"].startswith("ECHEC") for r in imgs),
            "nb_mises_de_cote": sum(r["statut"].startswith("MISE_DE_COTE") or r["statut"].startswith("NON_TRAITEE")
                                    for r in imgs),
            "nb_images_id_discordant": len(discordantes.get(canon, [])),
        }
        cpt = Counter(" ".join(x for x in (c["modalite"], c["lateralite"], c["orientation"]) if x)
                      for r in imgs for c in r.get("_contenu", []) if not c.get("erreur"))
        l["contenu_trouve"] = ", ".join(f"{k}={n}" for k, n in sorted(cpt.items()))
        types = types_par_id.get(p["id"], Counter()) if p else Counter()
        l["types_images_traitees"] = ", ".join(f"{t}={n}" for t, n in sorted(types.items()))
        if getattr(donnees, "centre_depuis", "dossier") == "donnees":
            l["centre_coherent"] = "N/A (centre pris dans les données)"
        elif p and p["centre"] and centres:
            nc = _norm_centre(p["centre"])
            l["centre_coherent"] = "Oui" if any(nc and (nc in _norm_centre(c) or _norm_centre(c) in nc)
                                                for c in centres) else "Non"
        else:
            l["centre_coherent"] = "N/A"

        if not p:
            l["statut_confrontation"] = "IMAGES_HORS_DONNEES"
            l["images_declarees_skezia"] = "N/A"
        elif donnees.cols_skezia:
            decl = {c: valeur_declaree(v) for c, v in p["skezia"].items()}
            declare = any(v is True for v in decl.values())
            l["images_declarees_skezia"] = "Oui" if declare else (
                "Non renseigné" if all(v is None for v in decl.values()) else "Non")
            l["detail_declaration_skezia"] = " | ".join(f"{c}={p['skezia'][c] or '∅'}" for c, _t, _o, _r in donnees.cols_skezia)
            if declare and nb_images_reelles:
                l["statut_confrontation"] = "SKEZIA_ET_FREDD"
            elif declare:
                l["statut_confrontation"] = "SKEZIA_SEULEMENT"
            elif nb_images_reelles:
                l["statut_confrontation"] = "FREDD_SEULEMENT"
            else:
                l["statut_confrontation"] = "NI_SKEZIA_NI_FREDD"
            # Regroupement par (type, œil, orientation) : plusieurs colonnes du
            # CRF peuvent désigner la même cible ; il suffit qu'une soit cochée.
            # « Trouvées » = contenu détecté à l'inventaire (fonctionne sans
            # raretia) ; « traitées » = sorties raretia. La présence est
            # établie si l'une OU l'autre source voit l'image.
            groupes = OrderedDict()
            for c, t, o, orient in donnees.cols_skezia:
                if t:
                    groupes.setdefault((t, o, orient), []).append(c)
            details, ecarts = [], []
            tov = types_oeil_par_id.get(p["id"], Counter())
            contenus = [c for r in imgs for c in r.get("_contenu", []) if not c.get("erreur")]
            contenus_disc = [c for r in discordantes.get(canon, []) for c in r.get("_contenu", []) if not c.get("erreur")]
            for (t, o, orient), cols in groupes.items():
                d = any(decl[c] is True for c in cols)
                if orient in ("maculaire", "grand_champ"):
                    # AF maculaire / grand champ : le champ ne se lit que sur les pixels traités.
                    lib = " ".join(x for x in (t, o, orient) if x)
                    cch = champ_par_id.get(p["id"], Counter())
                    n_ch = sum(v for (ty, oe, ch), v in cch.items() if ty.startswith(t) and (o is None or oe == o) and ch == orient)
                    n_ch_autre = sum(v for (ty, oe, ch), v in cch.items() if ty.startswith(t) and (o is None or oe == o)
                                     and ch and ch != orient)
                    n_ch_inconnu = sum(v for (ty, oe, ch), v in cch.items() if ty.startswith(t) and (o is None or oe == o)
                                       and not ch)
                    if not avec_raretia:
                        verdict = "NON_EVALUABLE_SANS_TRAITEMENT"
                    elif d == (n_ch > 0):
                        verdict = "OK"
                    elif d and n_ch_inconnu and not n_ch_autre:
                        verdict = "A_VERIFIER_LATERALITE_OU_ORIENTATION_INCONNUE"
                    elif d and n_ch_autre:
                        verdict = "DECLAREE_CHAMP_DIFFERENT"
                    else:
                        verdict = "DECLAREE_NON_TROUVEE" if d else "TROUVEE_NON_DECLAREE"
                    details.append(f"{lib}: déclaré={'oui' if d else 'non'} / trouvées={n_ch}"
                                   + (f" / autre champ={n_ch_autre}" if n_ch_autre else "")
                                   + (f" / champ inconnu={n_ch_inconnu}" if n_ch_inconnu else "") + f" -> {verdict}")
                    if verdict not in ("OK", "NON_EVALUABLE_SANS_TRAITEMENT"):
                        ecarts.append({"centre": p["centre"] or ", ".join(centres), "id_fredd": p["id"], "cible": lib,
                                       "declare": "oui" if d else "non", "trouvees": n_ch, "dont_piles": "",
                                       "autres_orientations": f"autre champ={n_ch_autre}" if n_ch_autre else "",
                                       "verdict": verdict})
                    continue

                def _ok_oeil(oe):
                    return o is None or oe == o

                def _ok_orient(ori):
                    # Une radiale ne valide ni « horizontale » ni « verticale ». « autre » = toute
                    # orientation connue qui n'est ni l'une ni l'autre.
                    if orient is None:
                        return True
                    if orient == "autre":
                        # Libellé du CRF : « Autre (volume, radiale, oblique etc) ». Un volume (pile) compte donc
                        # aussi dans « autre », même si ses lignes sont horizontales ou verticales (il y compte AUSSI
                        # dans « horizontale » / « verticale », voir plus haut).
                        return bool(ori) and (str(ori).startswith("volume")
                                              or ("horizontale" not in ori and "verticale" not in ori))
                    return orient in ori
                cible = [c for c in contenus if c["modalite"].startswith(t)]
                n_inv = sum(1 for c in cible if _ok_oeil(c["lateralite"]) and _ok_orient(c["orientation"]))
                n_piles = sum(1 for c in cible if _ok_oeil(c["lateralite"]) and _ok_orient(c["orientation"])
                              and str(c["orientation"]).startswith("volume"))
                autres = Counter(c["orientation"] for c in cible if orient and _ok_oeil(c["lateralite"])
                                 and c["orientation"] and not _ok_orient(c["orientation"]))
                n_tr = sum(v for (ty, oe), v in tov.items() if ty.startswith(t) and _ok_oeil(oe)) if orient is None else 0
                incertain = [c for c in cible if (o and c["lateralite"] in ("", "contradictoire"))
                             or (orient and _ok_oeil(c["lateralite"]) and not c["orientation"])]
                if o and orient is None:
                    incertain += [1] * sum(v for (ty, oe), v in tov.items() if ty.startswith(t) and oe == "")
                # Présence = contenu lu à l'inventaire (référence). Les sorties raretia
                # ne servent qu'en repli si le contenu n'a pas été analysé : combiner
                # deux classifieurs par un « ou » laisse passer les erreurs de l'un.
                present = n_inv > 0 if contenu_analyse else n_tr > 0
                verdict = ("OK" if d == present else "DECLAREE_NON_TROUVEE" if d else "TROUVEE_NON_DECLAREE")
                n_disc = sum(1 for c in contenus_disc if c["modalite"].startswith(t)
                             and (o is None or c["lateralite"] in (o, "", "contradictoire")))
                pile_seule = False
                if (verdict == "TROUVEE_NON_DECLAREE" and orient in ("horizontale", "verticale")
                        and n_inv and n_inv == n_piles):
                    # CRF : le libellé « Autre (volume, radiale…) » range les volumes dans « autre ». Des piles
                    # seules, non déclarées en horizontale / verticale, ne sont pas un écart ; l'écart éventuel est
                    # porté par la ligne « autre ».
                    verdict, pile_seule = "OK", True
                if verdict == "DECLAREE_NON_TROUVEE" and n_disc:
                    verdict = "A_VERIFIER_ID_DISCORDANT"
                elif verdict == "DECLAREE_NON_TROUVEE" and incertain:
                    verdict = "A_VERIFIER_LATERALITE_OU_ORIENTATION_INCONNUE"
                elif verdict == "DECLAREE_NON_TROUVEE" and autres:
                    verdict = "DECLAREE_ORIENTATION_DIFFERENTE"
                lib = " ".join(x for x in (t, o, orient) if x)
                details.append(f"{lib}: déclaré={'oui' if d else 'non'} / trouvées={n_inv}"
                               + (f" (dont piles={n_piles})" if n_piles else "")
                               + (" / piles seulement : relèvent de « autre »" if pile_seule else "")
                               + (f" / traitées={n_tr}" if orient is None and avec_raretia else "")
                               + (f" / ID discordant={n_disc}" if n_disc else "")
                               + (" / autres orientations trouvées : " + ", ".join(f"{k}={v}" for k, v in sorted(autres.items()))
                                  if verdict == "DECLAREE_ORIENTATION_DIFFERENTE" else "")
                               + f" -> {verdict}")
                if verdict != "OK":
                    ecarts.append({"centre": p["centre"] or ", ".join(centres), "id_fredd": p["id"],
                                   "cible": lib, "declare": "oui" if d else "non", "trouvees": n_inv,
                                   "dont_piles": n_piles,
                                   "autres_orientations": ", ".join(f"{k}={v}" for k, v in sorted(autres.items())),
                                   "verdict": verdict})
            # Contrôle de l'appareil déclaré (Clarus / Optos / Heidelberg) et de « AF et OCT dans le même E2E »
            extra = p.get("extra", {})
            for c, t, o in getattr(donnees, "cols_appareil", []):
                decl_f = appareil_declare(extra.get(c, ""), t)
                cible = [x for x in contenus if x["modalite"].startswith(t) and x["lateralite"] == o]
                if not decl_f or not cible:   # rien de déclaré, ou pas d'image : déjà traité plus haut
                    continue
                trouves = {appareil_trouve(x.get("appareil", "")) for x in cible} - {""}
                if not trouves:
                    verdict = "NON_EVALUABLE"
                else:
                    ok = decl_f in trouves if decl_f != "autre" else not (trouves & _APPAREILS_CONNUS)
                    verdict = "OK" if ok else "APPAREIL_DIFFERENT"
                lib = f"appareil {t} {o}"
                details.append(f"{lib}: déclaré={decl_f} / trouvé={', '.join(sorted(trouves)) or 'inconnu'} -> {verdict}")
                if verdict == "APPAREIL_DIFFERENT":
                    ecarts.append({"centre": p["centre"] or ", ".join(centres), "id_fredd": p["id"], "cible": lib,
                                   "declare": decl_f, "trouvees": ", ".join(sorted(trouves)), "dont_piles": "",
                                   "autres_orientations": "", "verdict": verdict})
            if getattr(donnees, "col_af_oct", None):
                v = valeur_declaree(extra.get(donnees.col_af_oct, ""))
                if v is not None:
                    def _a(r, m):
                        return any(x.get("modalite") == m and not x.get("erreur") for x in r.get("_contenu") or [])
                    af_ou_oct = any(_a(r, "autofluorescence") for r in imgs) and any(_a(r, "oct_bscan") for r in imgs)
                    meme = any(_a(r, "autofluorescence") and _a(r, "oct_bscan") for r in imgs
                               if r["categorie"] == "E2E_HEIDELBERG")
                    verdict = ("NON_EVALUABLE" if not af_ou_oct else "OK" if v == meme
                               else "DECLAREE_MEME_FICHIER_NON_TROUVE" if v else "MEME_FICHIER_NON_DECLARE")
                    lib = "AF dans le même E2E que l'OCT"
                    details.append(f"{lib}: déclaré={'oui' if v else 'non'} / trouvé={'oui' if meme else 'non'} -> {verdict}")
                    if verdict not in ("OK", "NON_EVALUABLE"):
                        ecarts.append({"centre": p["centre"] or ", ".join(centres), "id_fredd": p["id"], "cible": lib,
                                       "declare": "oui" if v else "non", "trouvees": "oui" if meme else "non",
                                       "dont_piles": "", "autres_orientations": "", "verdict": verdict})
            l["detail_par_modalite"] = " | ".join(details)
            l["nb_ecarts_modalite"] = sum(1 for x in details if not x.endswith(("-> OK", "-> NON_EVALUABLE_SANS_TRAITEMENT",
                                                                               "-> NON_EVALUABLE")))
            l["_ecarts"] = ecarts
        else:
            l["images_declarees_skezia"] = "N/A"
            l["statut_confrontation"] = "PRESENT_AVEC_IMAGES" if nb_images_reelles else "PRESENT_SANS_IMAGE"
        lignes.append(l)
    return lignes


CORRECTIONS_HEADERS = ["id_fredd", "cible", "declare_dans_le_CRF", "images_trouvees", "dont_piles",
                       "autres_orientations_trouvees", "constat", "correction_a_faire"]
_CONSTATS = {
    "DECLAREE_NON_TROUVEE": ("Déclaré dans le CRF, aucune image correspondante trouvée",
                             "Vérifier l'import des images pour ce patient, ou décocher dans le CRF."),
    "DECLAREE_ORIENTATION_DIFFERENTE": ("Déclaré dans le CRF, mais les OCT trouvés ont une autre orientation",
                                        "Corriger la saisie (horizontale / verticale / autre) ou importer la bonne coupe."),
    "TROUVEE_NON_DECLAREE": ("Images trouvées mais non déclarées dans le CRF", "Compléter la saisie du CRF."),
    "DECLAREE_CHAMP_DIFFERENT": ("AF déclarée dans un champ (maculaire / grand champ), l'AF trouvée est dans l'autre",
                                 "Corriger la saisie du champ ou importer la bonne AF."),
    "APPAREIL_DIFFERENT": ("Appareil déclaré dans le CRF différent de celui des images trouvées",
                           "Corriger l'appareil dans le CRF, ou vérifier que les bonnes images ont été importées."),
    "DECLAREE_MEME_FICHIER_NON_TROUVE": ("CRF : AF dans le même E2E que l'OCT, mais les fichiers trouvés sont séparés",
                                         "Corriger la réponse du CRF (non), ou importer le bon fichier E2E."),
    "MEME_FICHIER_NON_DECLARE": ("CRF : AF et OCT déclarés dans des fichiers distincts, mais ils sont dans le même E2E",
                                 "Corriger la réponse du CRF (oui)."),
    "A_VERIFIER_ID_DISCORDANT": ("Images retrouvées sous un identifiant discordant",
                                 "Trancher l'identité du patient avant de rattacher les images."),
    "A_VERIFIER_LATERALITE_OU_ORIENTATION_INCONNUE": ("Œil ou orientation non déterminés dans les images",
                                                      "Relire l'image à l'écran ; compléter à la main."),
}


def ecrire_corrections_centres(rap, confrontation):
    """Une liste de corrections par centre, à renvoyer aux ARC : « la vérité est dans les données »,
    les incohérences CRF ↔ images remontent aux centres. Identifiants pseudonymisés seulement."""
    par_centre = defaultdict(list)
    for l in confrontation:
        for e in l.get("_ecarts", []):
            constat, action = _CONSTATS.get(e["verdict"], (e["verdict"], ""))
            par_centre[e["centre"] or "centre_inconnu"].append({
                "id_fredd": e["id_fredd"], "cible": e["cible"], "declare_dans_le_CRF": e["declare"],
                "images_trouvees": e["trouvees"], "dont_piles": e["dont_piles"],
                "autres_orientations_trouvees": e["autres_orientations"], "constat": constat,
                "correction_a_faire": action})
    dossier = Path(rap) / "corrections_crf"
    if dossier.exists():
        shutil.rmtree(dossier, ignore_errors=True)   # pas de fichiers périmés d'une exécution précédente
    for centre, lignes in sorted(par_centre.items()):
        ecrire_csv(dossier / f"{safe_name(centre)}.csv", CORRECTIONS_HEADERS, lignes)
    return {c: len(v) for c, v in par_centre.items()}


def construire_actions(tot, conf, nb_q, incoh, taux, alertes, donnees):
    """Synthèse priorisée de ce qu'il y a à FAIRE à partir d'une exécution,
    plutôt que la simple description des sorties. Une ligne = une action
    concrète, avec le nombre concerné et le fichier/dossier précis à ouvrir.
    Retourne une liste de dicts (vide si rien à signaler)."""
    actions = []

    def ajouter(priorite, sujet, nb, fichier, action):
        if nb:
            actions.append({"priorite": priorite, "sujet": sujet, "nombre": nb,
                             "fichier_a_consulter": fichier, "action_recommandee": action})

    # BLOQUANT : à régler avant tout partage ou transfert
    ajouter("BLOQUANT", "DICOM en quarantaine (pseudonymisation non conforme)", nb_q,
            "quarantaine_pseudonymisation/",
            "Examiner chaque fichier avant tout partage : la sortie raretia n'a pas été correctement "
            "dépseudonymisée. Ne jamais transmettre en l'état.")
    if taux is not None and taux < 0.5:
        ajouter("BLOQUANT", "Taux de rattachement anormalement bas", f"{taux:.0%}", "rapport_execution.md (§ taux)",
                "Vérifier --col-id et la convention de nommage des dossiers avant d'aller plus loin : "
                "la majorité des images ne sont pas rattachées à un patient.")

    # À VÉRIFIER : le traitement raretia lui-même
    ajouter("A_VERIFIER", "Échecs de traitement (aucune sortie produite par raretia)",
            tot.get("ECHEC_TRAITEMENT", 0), "echecs_traitement/<centre>/<id_fredd>/",
            "Ouvrir rapports/journal_raretia/ pour chaque fichier et voir si l'échec est isolé ou "
            "systématique (à signaler si systématique).")
    ajouter("A_VERIFIER", "Images illisibles au moment de la copie (zip/disque)",
            tot.get("ECHEC_EXTRACTION", 0), "rapports/journal_images.csv (statut ECHEC_EXTRACTION)",
            "Vérifier l'intégrité de l'archive/zip source ; relancer après correction si besoin.")
    ajouter("A_VERIFIER", "Images traitées mais marquées « qualité limitée »",
            tot.get("TRAITEE_QUALITE_LIMITEE", 0), "images_traitees/… (voir rapports/sorties_traitees.csv)",
            "Revue qualité par un opérateur avant utilisation (image potentiellement sombre/illisible).")
    ajouter("A_VERIFIER", "Incohérences œil nom de fichier ↔ métadonnées", len(alertes),
            "01_inventaire/contenu_images.csv (colonne alerte)",
            "Vérifier manuellement quel œil est correct avant tout usage de ces images.")

    # À TRIER : images mises à l'écart, qui peuvent redevenir valides une fois les données à jour
    ajouter("A_TRIER", "Pseudonyme détecté mais absent des données FREDD",
            tot.get("MISE_DE_COTE_SANS_PATIENT", 0), "sans_patient/<centre>/",
            "Vérifier si le patient existe dans une mise à jour plus récente du CSV FREDD ; relancer si oui.")
    ajouter("A_TRIER", "Aucun pseudonyme détecté dans le chemin",
            tot.get("MISE_DE_COTE_ID_NON_DETECTE", 0), "sans_patient/<centre>/_id_non_detecte/",
            "Revue manuelle du nommage ; reclasser à la main si le patient est identifiable autrement.")
    ajouter("A_TRIER", "Identifiant ambigu (dossier/fichier discordants ou doublon CSV)",
            tot.get("MISE_DE_COTE_ID_AMBIGU", 0), "sans_patient/<centre>/_id_ambigu/",
            "Trancher manuellement l'identité avant tout rattachement : jamais d'automatisme sur ce cas.")
    ajouter("A_TRIER", "DICOM « Raw Data » (données propriétaires, sans image)",
            tot.get("NON_TRAITEE_RAW_DATA", 0), "non_traitees_format/<centre>/_raw_data/",
            "Normal pour les exports Zeiss : aucune action, sauf si ces données brutes doivent être conservées.")
    ajouter("A_TRIER", "Montages ultra-grand-champ reconstruits (natives conservées)",
            tot.get("NON_TRAITEE_MONTAGE", 0), "non_traitees_format/<centre>/_montage_reconstruit/",
            "Aucune action : les images natives sont dans images_traitees/ ; le montage reste dans l'environnement sécurisé.")
    ajouter("A_TRIER", "Angiographies FA / ICGA (hors périmètre FREDD)",
            tot.get("NON_TRAITEE_ANGIO", 0), "non_traitees_format/<centre>/_angiographie/",
            "Aucune action : conservées à part, hors images_traitees/.")
    ajouter("A_TRIER", "Fichiers DICOMDIR (index de support, sans image)",
            tot.get("NON_TRAITEE_DICOMDIR", 0), "non_traitees_format/<centre>/_dicomdir/",
            "Aucune action : ce ne sont pas des images (leurs DICOM sont traités à part).")
    ajouter("A_TRIER", "E2E uniquement infrarouge (IR exclues par décision FREDD)",
            tot.get("NON_TRAITEE_IR", 0), "non_traitees_format/<centre>/_infrarouge/",
            "Aucune action : conservés à part, hors images_traitees/.")
    ajouter("A_TRIER", "Format non pris en charge par raretia",
            tot.get("NON_TRAITEE_FORMAT", 0), "non_traitees_format/<centre>/<id>/",
            "Décider si ces formats doivent être convertis, ou exclus du périmètre FREDD.")

    # À INVESTIGUER : écarts de confrontation SKEZIA / FREDD
    ajouter("A_INVESTIGUER", "Images déclarées dans SKEZIA mais introuvables dans les dossiers",
            conf.get("SKEZIA_SEULEMENT", 0), "rapports/confrontation_patients.csv (statut SKEZIA_SEULEMENT)",
            "Vérifier si les images existent ailleurs (autre lot, autre support) ou si la déclaration est erronée.")
    ajouter("A_INVESTIGUER", "Images trouvées mais non déclarées dans SKEZIA",
            conf.get("FREDD_SEULEMENT", 0), "rapports/confrontation_patients.csv (statut FREDD_SEULEMENT)",
            "Vérifier si SKEZIA doit être mis à jour, ou si ces images sont hors-protocole.")
    ajouter("A_INVESTIGUER", "Patients sans aucune image déclarée ni retrouvée",
            conf.get("NI_SKEZIA_NI_FREDD", 0), "rapports/confrontation_patients.csv (statut NI_SKEZIA_NI_FREDD)",
            "Confirmer qu'aucune image n'est attendue pour ces patients (sortie d'étude, visite manquante...).")
    ajouter("A_INVESTIGUER", "Centre déclaré dans les données ≠ centre du dossier des images", incoh,
            "rapports/confrontation_patients.csv (colonne centre_coherent = Non)",
            "Vérifier une éventuelle erreur de centre dans le CSV FREDD ou un mauvais classement de dossier.")
    if not donnees.cols_skezia:
        ajouter("INFO", "Confrontation SKEZIA non activée (--cols-skezia absent)", 1, "—",
                "Si une confrontation détaillée par type d'image est souhaitée, fournir --cols-skezia.")
    return actions


SYNTHESE_EXECUTION_HEADERS = ["indicateur", "valeur", "explication"]


def construire_synthese_execution(recs, sorties_rows, confrontation, donnees):
    """Synthèse chiffrée globale (dédupliquée par patient), dans le même esprit
    que synthese_globale.csv de l'inventaire : des indicateurs de haut niveau à
    citer directement, plutôt que de recompter soi-même les CSV détaillés."""
    nb_sorties_ok = sum(1 for s in sorties_rows if s["rangement"] == "images_traitees")
    patients_avec_images = sum(1 for l in confrontation if int(l["nb_images_inventoriees"] or 0) > 0)
    patients_traites = sum(1 for l in confrontation if int(l["nb_images_traitees"] or 0) > 0)
    mod_inventoriees = Counter(c["modalite"] for r in recs for c in r.get("_contenu", []) if not c.get("erreur"))
    mod_traitees = Counter((s["image_type"] or "?") for s in sorties_rows if s["rangement"] == "images_traitees")
    # Qualité : comptée sur les seules images LIVRÉES. Le rapport brut de raretia
    # (raretia_bruts/*/rapport_complet.csv) porte, lui, sur toutes les sorties produites,
    # y compris celles ensuite mises en quarantaine : les deux totaux diffèrent du nombre
    # d'images en quarantaine, et ce sont ceux-ci qu'il faut citer.
    qual_livrees = Counter(s["acceptee"] for s in sorties_rows if s["rangement"] == "images_traitees")
    nb_quarantaine = sum(1 for s in sorties_rows if s["rangement"] == "quarantaine")
    fichiers_quarantaine_partielle = sum(1 for r in recs if r["statut"] == "QUARANTAINE_PARTIELLE")

    rows = [
        ("Nombre de patients FREDD dans les données (CSV)", len(donnees.patients),
         "Colonne identifiant utilisée : " + donnees.col_id + "."),
        ("Nombre de patients avec au moins une image retrouvée dans les dossiers", patients_avec_images,
         "Dédupliqué : un patient compte une fois même si ses images sont réparties sur plusieurs centres/lots."),
        ("Nombre de patients avec au moins une image effectivement traitée par raretia", patients_traites,
         "Au moins un DICOM produit avec un statut TRAITEE_* (OK, qualité limitée, ou qualité inconnue)."),
        ("Nombre total d'images (DICOM) produites et rangées dans images_traitees/", nb_sorties_ok,
         "Une image source peut produire plusieurs sorties (multi-frame, B-scans OCT) : ne pas confondre avec le "
         "nombre de fichiers sources."),
        ("Images par modalité, telles que détectées à l'inventaire (toutes, traitées ou non)",
         ", ".join(f"{k}={n}" for k, n in mod_inventoriees.most_common()) or "N/D (--pas-analyse-contenu ?)",
         "Détail par patient : colonne contenu_trouve de confrontation_patients.csv."),
        ("Images par type, parmi celles effectivement produites par raretia",
         ", ".join(f"{k}={n}" for k, n in mod_traitees.most_common()) or "N/D",
         "Détail par patient : colonne types_images_traitees de confrontation_patients.csv."),
        ("Qualité des images livrées", ", ".join(f"{k}={n}" for k, n in qual_livrees.most_common()) or "N/D",
         "Compté sur les seules images rangées dans images_traitees/. Le rapport brut de raretia "
         "(raretia_bruts/) compte en plus les images ensuite mises en quarantaine : l'écart entre les deux "
         "totaux vaut exactement le nombre d'images en quarantaine. Ce sont ces valeurs-ci qu'il faut citer."),
        ("Images mises en quarantaine pour défaut de pseudonymisation", nb_quarantaine,
         f"Compté image par image. Elles proviennent de "
         f"{len({s['chemin_source'] for s in sorties_rows if s['rangement'] == 'quarantaine'})} fichier(s) source, "
         f"dont {fichiers_quarantaine_partielle} ont par ailleurs des images livrées (statut "
         f"QUARANTAINE_PARTIELLE) : ne pas confondre le nombre de fichiers et le nombre d'images."),
    ]
    if donnees.cols_skezia:
        patients_declares_skezia = sum(1 for l in confrontation if l.get("images_declarees_skezia") == "Oui")
        patients_skezia_et_fredd = sum(1 for l in confrontation if l.get("statut_confrontation") == "SKEZIA_ET_FREDD")
        rows[1:1] = [
            ("Nombre de patients avec au moins une image déclarée dans SKEZIA", patients_declares_skezia,
             "D'après les colonnes --cols-skezia fournies ; 'Non renseigné' compté à part, voir "
             "confrontation_patients.csv."),
            ("… dont retrouvés avec au moins une image dans les dossiers (SKEZIA ET FREDD)",
             patients_skezia_et_fredd,
             "Le complément (déclarés mais introuvables) est le statut SKEZIA_SEULEMENT du rapport de "
             "confrontation — voir la section Actions à mener."),
        ]
    return rows


def empreintes_code_fredd():
    """SHA-256 des fichiers .py du pipeline FREDD (hors tests), tracés dans execution.json.
    Le numéro de version ne prouve pas quel code a tourné, l'empreinte le prouve (un tampon par fichier)."""
    import hashlib
    racine = Path(__file__).resolve().parent
    res = {}
    for f in sorted(racine.glob("*.py")):
        res[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return res


def ecrire_rapports(sortie, recs, sorties_rows, confrontation, erreurs_inv, paquets_log, meta, donnees):
    rap = sortie / "rapports"
    for r in recs:
        r["libelle_statut"] = STATUTS.get(r["statut"], r["statut"])
        if r.get("_log") and r["statut"] == "ECHEC_TRAITEMENT":
            r["observation"] = (r.get("observation", "") + f" | log : {r['_log']}").strip(" |")
    ecrire_csv(rap / "journal_images.csv", JOURNAL_HEADERS, recs)
    ecrire_csv(rap / "sorties_traitees.csv", SORTIES_HEADERS, sorties_rows)
    ecrire_csv(rap / "paquets_raretia.csv",
               ["paquet", "centre", "nb_images", "code_retour", "duree_s", "message", "log", "reessai_unitaire"],
               paquets_log)

    centres = sorted({r["centre"] for r in recs})
    cles = list(STATUTS)
    bilan = []
    for c in centres:
        rc = [r for r in recs if r["centre"] == c]
        cnt = Counter(r["statut"] for r in rc)
        ligne = {"centre": c, "nb_images_inventoriees": len(rc),
                 "nb_patients_fredd": len({r["id_fredd"] for r in rc if r["id_fredd"]}),
                 "nb_sorties_dicom": sum(1 for s in sorties_rows if s["centre"] == c and s["rangement"] == "images_traitees")}
        ligne.update({k: cnt.get(k, 0) for k in cles})
        bilan.append(ligne)
    ecrire_csv(rap / "bilan_par_centre.csv",
               ["centre", "nb_images_inventoriees", "nb_patients_fredd", "nb_sorties_dicom"] + cles, bilan)

    sk_headers = CONFRONTATION_HEADERS_BASE
    ecrire_csv(rap / "confrontation_patients.csv", sk_headers, confrontation)
    ecrire_corrections_centres(rap, confrontation)

    tot = Counter(r["statut"] for r in recs)
    conf = Counter(l["statut_confrontation"] for l in confrontation)
    nb_q_actions = sum(1 for s in sorties_rows if s["rangement"] == "quarantaine")
    incoh_actions = sum(1 for l in confrontation if l["centre_coherent"] == "Non")
    avec_id_actions = [r for r in recs if r["patient_id_detecte"]]
    taux_actions = (len([r for r in avec_id_actions if r["id_fredd"]]) / len(avec_id_actions)) \
        if avec_id_actions else None
    alertes_actions = [c["alerte"] for r in recs for c in r.get("_contenu", []) if c.get("alerte")]
    actions = construire_actions(tot, conf, nb_q_actions, incoh_actions, taux_actions, alertes_actions, donnees)
    ecrire_csv(rap / "actions_a_mener.csv",
               ["priorite", "sujet", "nombre", "fichier_a_consulter", "action_recommandee"], actions)
    synthese = construire_synthese_execution(recs, sorties_rows, confrontation, donnees)
    ecrire_csv(rap / "synthese_execution.csv", SYNTHESE_EXECUTION_HEADERS,
               [dict(zip(SYNTHESE_EXECUTION_HEADERS, row)) for row in synthese])

    L = []
    L.append(f"# Rapport d'exécution — pipeline images FREDD v{__version__}\n")
    L.append("## Synthèse chiffrée\n")
    L.append("| Indicateur | Valeur |\n|---|---:|")
    for indicateur, valeur, _explication in synthese:
        L.append(f"| {indicateur} | {valeur} |")
    L.append("\nExplications détaillées : `rapports/synthese_execution.csv`.\n")
    L.append("## Actions à mener\n")
    if actions:
        libelle_prio = {"BLOQUANT": "🔴 BLOQUANT (avant tout partage)", "A_VERIFIER": "🟠 À vérifier",
                         "A_TRIER": "🟡 À trier manuellement", "A_INVESTIGUER": "🔵 À investiguer",
                         "INFO": "⚪ Info"}
        ordre = ["BLOQUANT", "A_VERIFIER", "A_TRIER", "A_INVESTIGUER", "INFO"]
        for prio in ordre:
            lignes_prio = [a for a in actions if a["priorite"] == prio]
            if not lignes_prio:
                continue
            L.append(f"**{libelle_prio[prio]}**\n")
            for a in lignes_prio:
                L.append(f"- **{a['nombre']}** — {a['sujet']} → `{a['fichier_a_consulter']}`\n"
                         f"  *Action : {a['action_recommandee']}*")
            L.append("")
        L.append("Liste complète, à cocher au fur et à mesure : `rapports/actions_a_mener.csv`.\n")
    else:
        L.append("✅ Aucune action à signaler : pas d'échec, pas de mise à l'écart, pas d'écart de confrontation.\n")

    L.append(f"- Début : {meta['debut']}  \n- Fin : {meta['fin']}  \n- Durée : {meta['duree']}")
    L.append(f"- Mode : {'SIMULATION (aucune copie, aucun traitement)' if meta['simulation'] else 'exécution réelle'}")
    L.append(f"- Dossiers analysés : " + " ; ".join(meta["dossiers"]))
    L.append(f"- Données FREDD : `{meta['data']}` (encodage {donnees.encodage}) — {len(donnees.patients)} patients, "
             f"colonne identifiant `{donnees.col_id}`, colonne centre `{donnees.col_centre or '—'}`, "
             f"colonnes SKEZIA : {', '.join(':'.join(x for x in q if x) for q in donnees.cols_skezia) or '—'}")
    L.append("- Centre des images : " + ("pris dans les données FREDD (images sans patient : dossier d'origine)"
             if meta.get("centre_depuis") == "donnees" else "dossier de 1er niveau sous le dossier analysé"))
    if donnees.cols_skezia_absentes:
        L.append(f"- ⚠ Colonnes du CRF FREDD absentes de l'export (non confrontées) : "
                 + ", ".join(donnees.cols_skezia_absentes))
    if donnees.lignes_sans_id:
        L.append(f"- ⚠ {donnees.lignes_sans_id} ligne(s) du CSV sans identifiant exploitable (ignorées)")
    if donnees.ambigus:
        L.append(f"- ⚠ {len(donnees.ambigus)} identifiant(s) ambigu(s) dans le CSV (même préfixe 8 caractères pour "
                 f"plusieurs patients) : images correspondantes mises de côté")
    if donnees.ids_format_inattendu:
        L.append(f"- ⚠ **{donnees.ids_format_inattendu} identifiant(s) du CSV ne sont ni un UUID ni 8 caractères "
                 f"hexadécimaux** : la colonne `{donnees.col_id}` est-elle bien l'ID SKEZIA (`--col-id`) ?")
    if donnees.lignes_fusionnees:
        L.append(f"- {donnees.lignes_fusionnees} ligne(s) supplémentaire(s) d'un même patient (visites) fusionnée(s) : "
                 f"une image déclarée à n'importe quelle ligne compte comme déclarée")
    avec_id = [r for r in recs if r["patient_id_detecte"]]
    rattachees = [r for r in avec_id if r["id_fredd"]]
    taux = (len(rattachees) / len(avec_id)) if avec_id else None
    if avec_id:
        L.append(f"- Taux de rattachement : **{taux:.0%}** des images portant un ID sont rattachées à un patient "
                 f"FREDD ({len(rattachees)}/{len(avec_id)})" +
                 (" — ⚠ **taux anormalement bas : vérifier `--col-id` et le nommage des dossiers avant d'aller "
                  "plus loin**" if taux < 0.5 else ""))
    alertes = [(r["chemin_source"], c["alerte"]) for r in recs for c in r.get("_contenu", []) if c.get("alerte")]
    if alertes:
        L.append(f"- ⚠ {len(alertes)} incohérence(s) de métadonnées (ex. œil du nom ≠ œil du fichier) : colonne "
                 f"`alerte` de `01_inventaire/contenu_images.csv`")
    rv = meta["raretia"]
    L.append(f"- raretia : version `{rv.get('version')}`, dossier `{rv.get('dossier_package', rv.get('chemin'))}`, "
             f"empreinte du code `{rv.get('sha256_code') or 'n/d'}`" + (f" — ⚠ {rv['erreur']}" if rv.get("erreur") else ""))
    L.append(f"- Environnement : Python {platform.python_version()} ({platform.platform()}), "
             f"pydicom {getattr(pydicom, '__version__', 'absent')}")
    L.append(f"- Commande : `{meta['commande']}`\n")

    L.append("## 1. Inventaire\n")
    L.append(f"- Images inventoriées : **{len(recs)}** (journal complet : `rapports/journal_images.csv`)")
    L.append(f"- Erreurs de lecture pendant l'inventaire (zips corrompus, accès refusé…) : **{len(erreurs_inv)}** "
             f"(`01_inventaire/erreurs_inventaire_*.csv`)")
    L.append(f"- Centres détectés : {len(centres)} — " + ", ".join(centres) + "\n")

    L.append("## 2–3. Devenir de chaque image\n")
    L.append("| Statut | Nombre | Signification |\n|---|---:|---|")
    for k, lib in STATUTS.items():
        if tot.get(k):
            L.append(f"| `{k}` | {tot[k]} | {lib} |")
    L.append(f"| **Total** | **{len(recs)}** | |\n")
    nb_sorties_ok = sum(1 for s in sorties_rows if s["rangement"] == "images_traitees")
    L.append(f"- DICOM produits et rangés dans `images_traitees/<centre>/<date>/<patient>/` : **{nb_sorties_ok}** "
             f"(une image source peut en produire plusieurs : multi-frame, B-scans OCT)")
    date_t = meta.get("date_traitement")
    if date_t and not meta["simulation"]:
        nb_nouv = sum(1 for s in sorties_rows if s["rangement"] == "images_traitees"
                      and Path(s["fichier_sortie"]).parts[2:3] == (date_t,))
        L.append(f"- Dont produits par ce passage, dans `images_traitees/<centre>/{date_t}/` : **{nb_nouv}** "
                 f"(dossiers à transmettre tels quels ; les images reprises restent dans leur dossier de date d'origine)")
    nb_q = sum(1 for s in sorties_rows if s["rangement"] == "quarantaine")
    if nb_q:
        L.append(f"- ⚠ **{nb_q} DICOM en quarantaine** pour défaut de pseudonymisation : à examiner AVANT tout partage")
    L.append("")

    L.append("### Bilan par centre\n")
    cols_aff = [k for k in cles if tot.get(k)]
    L.append("| Centre | Images | Patients FREDD | DICOM produits | " + " | ".join(f"`{k}`" for k in cols_aff) + " |")
    L.append("|---|---:|---:|---:|" + "---:|" * len(cols_aff))
    for b in bilan:
        L.append(f"| {b['centre']} | {b['nb_images_inventoriees']} | {b['nb_patients_fredd']} | {b['nb_sorties_dicom']} | "
                 + " | ".join(str(b[k]) for k in cols_aff) + " |")
    L.append("")

    if paquets_log:
        echecs = [p for p in paquets_log if p["code_retour"] != 0]
        L.append(f"### Appels raretia\n\n- Paquets lancés : {len(paquets_log)} ; en échec global : {len(echecs)} "
                 f"(détail : `rapports/paquets_raretia.csv`, logs : `rapports/journal_raretia/`)")
        for p in echecs[:20]:
            L.append(f"  - `{p['paquet']}` : code {p['code_retour']} — {p['message']}")
        L.append("")

    a_voir = [r for r in recs if r["statut"] in ("ECHEC_TRAITEMENT", "ECHEC_PSEUDONYMISATION",
                                                 "QUARANTAINE_PARTIELLE", "ECHEC_EXTRACTION")]
    if a_voir:
        L.append(f"### Échecs ({len(a_voir)})\n")
        for r in a_voir[:50]:
            L.append(f"- `{r['statut']}` — {r['chemin_source']} — {r.get('motif', '')}")
        if len(a_voir) > 50:
            L.append(f"- … {len(a_voir) - 50} autre(s) : voir `journal_images.csv`")
        L.append("")

    L.append("## 5. Confrontation données FREDD / SKEZIA ↔ images retrouvées\n")
    L.append("Détail par patient : `rapports/confrontation_patients.csv`.\n")
    L.append("| Statut | Patients | Signification |\n|---|---:|---|")
    for k, lib in CONFRONTATION_LIBELLES.items():
        if conf.get(k):
            L.append(f"| `{k}` | {conf[k]} | {lib} |")
    if not donnees.cols_skezia:
        L.append("\n⚠ Aucune colonne SKEZIA fournie (`--cols-skezia`) : la confrontation se limite à "
                 "présence dans les données ↔ images retrouvées.")
    incoh = sum(1 for l in confrontation if l["centre_coherent"] == "Non")
    if incoh:
        L.append(f"\n⚠ {incoh} patient(s) dont le centre des données ne correspond pas au dossier-centre des images.")
    L.append("\n## Arborescence de sortie\n")
    L.append("```\n"
             "images_traitees/<centre>/<date>/<id_fredd>/  DICOM pseudonymisés et contrôlés ; <date> =\n"
             "                                       jour du passage qui les a produits (AAAA-MM-JJ)\n"
             "sans_patient/<centre>/                 pseudonyme absent des données FREDD\n"
             "sans_patient/<centre>/_id_non_detecte/ aucun pseudonyme détecté\n"
             "sans_patient/<centre>/_id_ambigu/      pseudonyme ambigu\n"
             "non_traitees_format/<centre>/<id>/     jpg/png/tiff… (non pris en charge par raretia)\n"
             "echecs_traitement/<centre>/<id>/       sources que raretia n'a pas pu traiter\n"
             "quarantaine_pseudonymisation/          sorties raretia non conformes\n"
             "01_inventaire/                         rapports de l'inventaire\n"
             "rapports/                              ce rapport + CSV détaillés + logs raretia\n"
             "```\n")
    (rap / "rapport_execution.md").write_text("\n".join(L), encoding="utf-8")
    resume = {"version_pipeline": __version__, "empreintes_code_fredd": empreintes_code_fredd(),
              **{k: v for k, v in meta.items()},
              "statuts": dict(tot), "confrontation": dict(conf), "nb_dicom_produits": nb_sorties_ok,
              "nb_quarantaine": nb_q}
    (rap / "execution.json").write_text(json.dumps(resume, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return rap / "rapport_execution.md"


# Statuts d'étape 3 considérés comme « déjà traité » : on ne les renvoie JAMAIS
# à raretia sur un passage suivant (sauf --refaire-tout). Les statuts d'étape 2
# (mise à l'écart, format non pris en charge) n'y figurent PAS volontairement :
# ils doivent être ré-évalués à CHAQUE passage, car un patient absent des
# données FREDD aujourd'hui peut y apparaître demain (CSV mis à jour).
STATUTS_REPRISE = STATUTS_TRAITES | {"ECHEC_TRAITEMENT", "ECHEC_PSEUDONYMISATION", "ECHEC_EXTRACTION"}

ETAT_TRAITEMENT_HEADERS = ["sha256", "patient_id_detecte", "statut", "id_fredd", "centre", "chemin_source", "motif",
                           "date_traitement", "version_pipeline"]


def cle_reprise(r):
    """Clé de l'état de reprise = (empreinte, patient détecté), la MÊME clé que le
    dédoublonnage de l'étape 2 : un même fichier déposé pour deux patients a deux lignes d'état
    distinctes. Vide si l'empreinte est inconnue."""
    sha = r.get("sha256") or ""
    return f"{sha}|{r.get('patient_id_detecte') or ''}" if sha else ""


def _cle_ligne_etat(ligne, sha_col):
    """Clé d'une ligne relue dans un fichier d'état. Un état ancien sans colonne
    patient_id_detecte garde pour clé l'empreinte seule (repli de lecture, voir _chercher_etat)."""
    sha = ligne.get(sha_col) or ""
    if not sha:
        return ""
    return f"{sha}|{ligne['patient_id_detecte']}" if "patient_id_detecte" in ligne else sha


def _chercher_etat(dico, r):
    """Résultat mémorisé pour l'enregistrement r : clé (empreinte, patient), sinon repli sur
    l'ancienne clé (empreinte seule) d'un état ancien."""
    cle = cle_reprise(r)
    if not cle:
        return None
    for k in (cle, r["sha256"], r["sha256"] + "|"):   # « sha| » : ligne d'un état ancien réécrite
        if k in dico:
            return dico[k]
    return None


def lire_etat_precedent(sortie):
    """Relit l'état d'un passage précédent dans le MÊME dossier --sortie (dossier
    neuf ou première exécution -> état vide, ce n'est pas une erreur).
    Retourne (etat: {sha256: ligne}, sorties_par_sha: {sha256: [ligne sortie, ...]})."""
    etat = {}
    p_etat = sortie / "rapports" / "etat_traitement.csv"
    if p_etat.exists():
        with open(p_etat, "r", encoding="utf-8-sig", newline="") as f:
            for ligne in csv.DictReader(f, delimiter=";"):
                cle = _cle_ligne_etat(ligne, "sha256")
                if ligne.get("statut") == "TRAITEE_A_VERIFIER":   # ancien libellé (<= 1.12.1)
                    ligne["statut"] = "TRAITEE_QUALITE_LIMITEE"
                if cle:
                    etat[cle] = ligne
    sorties_par_sha = defaultdict(list)
    p_sorties = sortie / "rapports" / "etat_sorties.csv"
    if p_sorties.exists():
        with open(p_sorties, "r", encoding="utf-8-sig", newline="") as f:
            for ligne in csv.DictReader(f, delimiter=";"):
                cle = _cle_ligne_etat(ligne, "sha256_source")
                if ligne.get("acceptee") == "À VÉRIFIER":          # ancien libellé (<= 1.12.1)
                    ligne["acceptee"] = "QUALITÉ LIMITÉE"
                ligne.pop("sha256_source", None)
                ligne.pop("patient_id_detecte", None)
                if cle:
                    sorties_par_sha[cle].append(ligne)
    return etat, sorties_par_sha


def appliquer_reprise(recs, etat_prec, opts):
    """Pour chaque image qui serait normalement envoyée à raretia (statut
    A_TRAITER), si son empreinte a déjà un résultat d'étape 3 d'un passage
    précédent, on reprend ce résultat tel quel au lieu de la retraiter.
    Ne fait rien en --simulation (rien n'est jamais réellement traité) ni en
    --refaire-tout (on veut explicitement tout repasser dans raretia).
    Retourne la liste des recs repris (pour fusionner leurs anciennes sorties)."""
    if opts.simulation or opts.refaire_tout or not etat_prec:
        return []
    repris = []
    for r in recs:
        if r["statut"] != "A_TRAITER":
            continue
        prec = _chercher_etat(etat_prec, r)
        if prec and prec.get("statut") in STATUTS_REPRISE:
            r["statut"] = prec["statut"]
            r["motif"] = (prec.get("motif") or "")
            r["observation"] = (r.get("observation", "") +
                                f" | déjà traité le {prec.get('date_traitement', '?')} (reprise, non "
                                f"retraité ; --refaire-tout pour forcer)").strip(" |")
            r["_reprise"] = True
            repris.append(r)
    return repris


def sauvegarder_etat(sortie, recs, sorties_rows_nouvelles, etat_prec, sorties_prec_par_sha, opts, version):
    """Met à jour rapports/etat_traitement.csv et rapports/etat_sorties.csv pour
    le prochain passage. Superset : les entrées d'un sha256 non revu lors de ce
    passage (fichier retiré du dossier source entre deux exécutions) sont
    conservées, pas supprimées -- l'état ne fait qu'accumuler."""
    if opts.simulation:
        return  # rien n'a été réellement traité : ne jamais écrire d'état depuis une simulation
    date = datetime.now().isoformat(timespec="seconds")
    etat = dict(etat_prec)
    for r in recs:
        if r.get("_reprise") or r["statut"] not in STATUTS_REPRISE or not r["sha256"]:
            continue
        etat[cle_reprise(r)] = {"sha256": r["sha256"], "patient_id_detecte": r.get("patient_id_detecte") or "",
                              "statut": r["statut"], "id_fredd": r["id_fredd"],
                              "centre": r["centre"], "chemin_source": r["chemin_source"],
                              "motif": r.get("motif", ""), "date_traitement": date, "version_pipeline": version}
    ecrire_csv(sortie / "rapports" / "etat_traitement.csv", ETAT_TRAITEMENT_HEADERS, list(etat.values()))

    recs_par_chemin = {r["chemin_source"]: r for r in recs}
    sorties_par_sha = defaultdict(list, {k: list(v) for k, v in sorties_prec_par_sha.items()})
    for ligne in sorties_rows_nouvelles:
        r = recs_par_chemin.get(ligne["chemin_source"])
        if r and r["sha256"] and not r.get("_reprise"):
            sorties_par_sha[cle_reprise(r)].append(ligne)
    lignes_plates = []
    for cle, lignes in sorties_par_sha.items():
        sha, _, pid = cle.partition("|")
        lignes_plates.extend(dict(ligne, sha256_source=sha, patient_id_detecte=pid) for ligne in lignes)
    ecrire_csv(sortie / "rapports" / "etat_sorties.csv", ["sha256_source", "patient_id_detecte"] + SORTIES_HEADERS,
               lignes_plates)


def _date_iso(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"date attendue au format AAAA-MM-JJ : {v!r}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Pipeline images FREDD : inventaire -> tri -> raretia par centre -> rapports -> confrontation.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dossiers", nargs="+", help="Dossier(s) racine(s) ; chaque sous-dossier de 1er niveau = un centre.")
    p.add_argument("--data", required=True, type=Path, help="CSV des données FREDD (ex. data_clean.csv).")
    p.add_argument("--sortie", required=True, type=Path, help="Dossier de sortie (créé si besoin).")
    g = p.add_argument_group("Colonnes du CSV")
    g.add_argument("--col-id", default=None, help=f"Colonne du pseudonyme (défaut : 1re trouvée parmi {ID_COLUMN_CANDIDATES}).")
    g.add_argument("--col-centre", default=None,
                   help="Colonne du centre. Si elle est connue, le centre de chaque image est pris dans les "
                        "données (voir --centre-depuis).")
    g.add_argument("--alias-centres", default=None,
                   help="Noms de centres à fusionner : CSV « nom_source;nom_retenu » ou texte 'HUS=CARGO-HUS;...'. "
                        "Défaut : alias_centres.csv à côté du script s'il existe ; 'aucun' pour désactiver.")
    g.add_argument("--centre-depuis", choices=["donnees", "dossier"], default=None,
                   help="'donnees' (défaut si une colonne centre est trouvée) : centre du patient dans le CSV, "
                        "quelle que soit l'arborescence (dépôts rangés par lot). 'dossier' : dossier de 1er "
                        "niveau sous le dossier scanné (dépôts rangés par centre).")
    g.add_argument("--cols-skezia", default="",
                   help="'fredd' = correspondance prête pour le CRF FREDD (page_depot_images : fond d'œil, "
                        "OCT, autofluorescence, par œil). Sinon colonnes séparées par des virgules, au format "
                        "colonne[:type_raretia[:OD|OG]], ex. 'img_oct_od:oct_bscan:OD,img_fo:fundus'.")
    g = p.add_argument_group("raretia (module pipeline_wp2)")
    g.add_argument("--raretia-path", default=str(Path(__file__).resolve().parent),
                   help="Dossier CONTENANT le package pipeline_wp2/ (défaut : le dossier de ce script, "
                        "où le module pipeline_wp2 est livré).")
    g.add_argument("--raretia-module", default="pipeline_wp2.main",
                    help="Module CLI à lancer (défaut : pipeline_wp2.main — nom réel du package).")
    g.add_argument("--raretia-args", default="", help="Options supplémentaires passées à raretia, ex. '--target-size 2048'.")
    g.add_argument("--taille-paquet", type=int, default=200, help="Nombre d'images par appel raretia (défaut : 200).")
    g.add_argument("--timeout-paquet", type=int, default=None, help="Durée max (s) d'un appel raretia.")
    g.add_argument("--pas-de-reessai-unitaire", action="store_true",
                   help="Ne pas relancer image par image un paquet en échec.")
    g.add_argument("--sans-controle-texte", dest="controle_texte", action="store_false",
                   help="Désactive le contrôle du texte incrusté dans les pixels (actif par défaut : une image "
                        "suspecte part en quarantaine_pseudonymisation/).")
    g.add_argument("--garder-patientid-raretia", action="store_true",
                   help="Ne pas remplacer le PatientID technique (<id>__<empreinte>) par l'identifiant FREDD.")
    g = p.add_argument_group("Reprise (relances régulières sur le même --sortie)")
    g.add_argument("--date-traitement", type=_date_iso, default=None, metavar="AAAA-MM-JJ",
                   help="Nom du sous-dossier de date : images_traitees/<centre>/<date>/<id>/ (défaut : date du "
                        "lancement). Les images reprises d'un passage précédent restent dans leur dossier d'origine.")
    g.add_argument("--refaire-tout", action="store_true",
                   help="Retraiter même les images déjà traitées (ou déjà en échec) lors d'un passage précédent "
                        "dans ce même dossier de sortie. Sans cette option, seules les images NOUVELLES (jamais "
                        "vues, par empreinte SHA-256) sont envoyées à raretia -- utile pour relancer régulièrement "
                        "sur un centre sans tout refaire. Toujours utiliser après un changement de version du "
                        "pipeline ou de raretia : une image déjà traitée n'est jamais revérifiée automatiquement.")
    g = p.add_argument_group("Échantillon")
    g.add_argument("--echantillon", type=Path, default=None, metavar="echantillon_patients.csv",
                   help="Ne traiter que les patients listés (fichier produit par « fredd.py echantillon »). Les "
                        "autres images restent inventoriées, avec le statut HORS_ECHANTILLON, et ne sont pas "
                        "mémorisées par la reprise : une exécution ultérieure sans cette option les traitera.")
    g = p.add_argument_group("Divers")
    g.add_argument("--simulation", action="store_true", help="Inventaire + tri + rapports, sans copie ni traitement.")
    g.add_argument("--pas-analyse-contenu", action="store_true",
                   help="Ne pas analyser le contenu des E2E/DICOM (modalité, œil, orientation) : plus rapide, "
                        "mais la confrontation SKEZIA ne reposera que sur les sorties raretia.")
    g.add_argument("--garder-doublons", action="store_true", help="Traiter aussi les copies binaires identiques.")
    g.add_argument("--dossier-travail", default=None, help="Dossier temporaire (défaut : <sortie>/_travail).")
    g.add_argument("--conserver-travail", action="store_true", help="Ne pas supprimer les fichiers temporaires.")
    g.add_argument("--extensions-image-suppl", default="")
    g.add_argument("--profondeur-zip-max", type=int, default=10)
    g.add_argument("--taille-max-zip-memoire-mo", type=int, default=200)
    return p.parse_args(argv)


def main(argv=None):
    opts = parse_args(argv)
    t0 = time.time()
    debut = datetime.now().isoformat(timespec="seconds")
    if not opts.date_traitement:  # un seul dossier de date par lancement, même s'il passe minuit
        opts.date_traitement = debut[:10]
    sortie = opts.sortie.resolve()

    # Garde-fou : la sortie ne doit pas être à l'intérieur d'un dossier scanné
    for d in opts.dossiers:
        try:
            sortie.relative_to(Path(d).resolve())
            print(f"[ERREUR] Le dossier de sortie ({sortie}) est à l'intérieur du dossier analysé ({d}) : "
                  f"il serait ré-inventorié au prochain passage. Choisir une sortie ailleurs.")
            return 2
        except ValueError:
            pass
    etat_connu = (sortie / "rapports" / "etat_traitement.csv").exists()
    if sortie.exists() and any(sortie.iterdir()) and not opts.simulation and not etat_connu:
        if any((sortie / n).exists() for n in ("images_traitees", "sans_patient", "rapports")):
            print(f"[ATTENTION] Le dossier de sortie {sortie} contient déjà des résultats, mais sans "
                  f"rapports/etat_traitement.csv (résultats d'une version antérieure du pipeline, sans reprise, "
                  f"ou dossier mal choisi ?). Les images seront retraitées : leur état n'est pas connu. Utiliser "
                  f"un dossier neuf pour une exécution isolée, ou vérifier qu'il s'agit bien du bon dossier de "
                  f"reprise pour ce centre.")
    sortie.mkdir(parents=True, exist_ok=True)

    try:
        donnees = DonneesFredd.charger(opts.data, opts.col_id, opts.col_centre, opts.cols_skezia)
    except (ValueError, OSError) as e:
        print(f"[ERREUR] Données FREDD : {e}")
        return 2
    print(f"Données FREDD : {len(donnees.patients)} patients (colonne '{donnees.col_id}').")
    try:
        alias, origine_alias = charger_alias_centres(opts.alias_centres)
    except OSError as e:
        print(f"[ERREUR] Correspondances de centres illisibles : {e}")
        return 2
    donnees.alias_centres = alias
    if alias:
        n_modif = 0
        for p in donnees.patients.values():
            c = canon_centre(p["centre"], alias)
            n_modif += c != p["centre"]
            p["centre"] = c
        print(f"Correspondances de centres ({origine_alias}) : {len(alias)} règle(s), "
              f"{n_modif} patient(s) concerné(s).")

    raretia_info = {"version": "non utilisé (simulation)"} if opts.simulation else \
        ra.version_raretia(opts.raretia_path, opts.raretia_module.split(".")[0], module_cli=opts.raretia_module)
    if not opts.simulation and raretia_info.get("erreur"):
        print(f"[ERREUR] Module pipeline_wp2 inutilisable : {raretia_info['erreur']}\n"
              f"         Vérifier les dépendances (pip install -r requirements.txt), --raretia-path, ou lancer --simulation.")
        return 2

    print("\n##### ÉTAPE 1 — INVENTAIRE #####")
    lignes_inv, erreurs_inv, _inv_path = etape1_inventaire(
        opts.dossiers, sortie / "01_inventaire", opts, set(donnees.patients))
    if not lignes_inv:
        print(f"\n[ERREUR] Aucune image trouvée dans : {', '.join(map(str, opts.dossiers))}\n"
              f"         Vérifier le chemin (dossier vide ? zip copié ailleurs ?).")
        return 3

    print("\n##### ÉTAPE 2 — TRI / MISE À L'ÉCART #####")
    centre_depuis = opts.centre_depuis or ("donnees" if donnees.col_centre else "dossier")
    donnees.centre_depuis = centre_depuis
    print(f"  Centre de chaque image : " + (f"colonne '{donnees.col_centre}' des données FREDD "
          f"(images sans patient : dossier d'origine)" if centre_depuis == "donnees"
          else "dossier de 1er niveau sous le dossier scanné"))
    recs = etape2_tri(lignes_inv, donnees, opts.garder_doublons, centre_depuis)
    if opts.echantillon:
        try:
            ech = ech_mod.lire_echantillon(opts.echantillon)
        except (OSError, ValueError) as e:
            print(f"[ERREUR] Échantillon illisible : {e}")
            return 2
        hors = 0
        for r in recs:
            if r["statut"] == "A_TRAITER" and r["id_fredd"] not in ech:
                r["statut"] = "HORS_ECHANTILLON"
                hors += 1
        print(f"  Échantillon ({opts.echantillon.name}) : {len(ech)} patient(s) retenu(s) ; "
              f"{hors} image(s) d'autres patients laissées de côté pour cette exécution.")
    lecteur = LecteurSources(opts.taille_max_zip_memoire_mo)
    paquets_log, sorties_rows = [], []
    try:
        if not opts.pas_analyse_contenu:
            print("\n##### ÉTAPE 1b — CONTENU DES FICHIERS (modalité, œil, orientation) #####")
            contenu = etape1b_contenu(recs, lecteur, sortie, opts)
            c = Counter(x["modalite"] for x in contenu if not x["erreur"])
            print("  " + ", ".join(f"{k}={v}" for k, v in c.most_common()) +
                  f" ; illisibles={sum(1 for x in contenu if x['erreur'])}")
            marquer_raw_data(recs)
            marquer_montages(recs)
        mettre_de_cote(recs, lecteur, sortie, opts.simulation)
        c = Counter(r["statut"] for r in recs)
        print("  " + ", ".join(f"{k}={v}" for k, v in c.items()))

        # Reprise : ne pas renvoyer à raretia ce qui a déjà un résultat d'un passage précédent
        etat_prec, sorties_prec_par_sha = lire_etat_precedent(sortie)
        repris = appliquer_reprise(recs, etat_prec, opts)
        if repris:
            print(f"  ↻ {len(repris)} image(s) déjà traitée(s) lors d'un passage précédent : non "
                  f"retraitée(s) (--refaire-tout pour forcer).")

        print("\n##### ÉTAPE 3 — PRÉTRAITEMENT RARETIA #####")
        sorties_rows = etape3_traitement(
            recs, lecteur, sortie, opts, paquets_log,
            checkpoint=lambda rows: sauvegarder_etat(sortie, recs, rows, etat_prec, sorties_prec_par_sha,
                                                     opts, __version__))
        for r in repris:
            sorties_rows.extend(_chercher_etat(sorties_prec_par_sha, r) or [])
    finally:
        lecteur.fermer()

    sauvegarder_etat(sortie, recs, sorties_rows, etat_prec, sorties_prec_par_sha, opts, __version__)

    # 5. Confrontation (avant l'écriture des rapports, qui l'inclut)
    confrontation = etape5_confrontation(recs, sorties_rows, donnees)

    meta = {"debut": debut, "fin": datetime.now().isoformat(timespec="seconds"),
            "duree": f"{time.time() - t0:.1f} s", "simulation": opts.simulation,
            "date_traitement": opts.date_traitement,
            "dossiers": [str(Path(d).resolve()) for d in opts.dossiers], "data": str(opts.data),
            "raretia": raretia_info, "centre_depuis": centre_depuis, "alias_centres": getattr(donnees, "alias_centres", {}), "commande": " ".join([Path(sys.argv[0]).name] + (argv or sys.argv[1:]))}
    rapport = ecrire_rapports(sortie, recs, sorties_rows, confrontation, erreurs_inv, paquets_log, meta, donnees)

    tot = Counter(r["statut"] for r in recs)
    print("\n" + "=" * 70 + "\n  RÉSUMÉ\n" + "=" * 70)
    for k in STATUTS:
        if tot.get(k):
            print(f"  {k:<30} {tot[k]}")
    print(f"  {'TOTAL':<30} {len(recs)}")
    print(f"\n  Rapport : {rapport}")
    if not opts.simulation:
        import catalogue_images as cat
        bilan = cat.construire(sortie, log=lambda *_: None)
        if bilan:
            cat.afficher(bilan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
