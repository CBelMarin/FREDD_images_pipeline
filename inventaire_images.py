#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inventaire_images.py
=====================

Fait l'inventaire de toutes les images FREDD (DICOM .dcm, Heidelberg .e2e,
et images standards .jpg/.png/.tiff/...) présentes sous un ou plusieurs
dossiers, y compris :

  - dans des arborescences très profondes (chemins Windows > 260 caractères
    pris en charge automatiquement via le préfixe \\\\?\\) ;
  - à l'intérieur de fichiers .zip, y compris des .zip imbriqués dans
    d'autres .zip (poupées russes) ;
  - même quand un .zip est corrompu, protégé par mot de passe, ou dans un
    format « propriétaire » que le module zip standard ne sait pas lire :
    dans ce cas le fichier est listé dans le rapport d'erreurs avec un
    message explicite, au lieu de faire planter le script.

Ce script est STRICTEMENT EN LECTURE SEULE : contrairement à analyze.py, il
ne modifie, ne renomme, ne déplace et ne supprime AUCUN fichier d'origine.
Les zips imbriqués sont uniquement inspectés en mémoire (ou dans un fichier
temporaire supprimé aussitôt après lecture) pour lister leur contenu.

Aucune dépendance externe : uniquement la bibliothèque standard de Python
(3.8+). Rien à installer avec pip.

RÉSULTATS PRODUITS (dans le dossier de sortie, par défaut le dossier courant)
  - inventaire_images_<horodatage>.csv   : une ligne par image trouvée
  - erreurs_inventaire_<horodatage>.csv  : tout ce qui n'a pas pu être lu
  - bilan_par_lot_<horodatage>.csv       : bilan patients/images par lot
                                            (= sous-dossier de 1er niveau
                                            sous chaque dossier scanné)
  - patients_multi_lots_<horodatage>.csv : patients présents dans plusieurs
                                            lots, avec indication précise si
                                            ce sont les mêmes images (copies
                                            identiques) ou des images
                                            différentes entre les lots.

Les CSV sont écrits en UTF-8 avec BOM et séparateur ';' pour s'ouvrir
correctement dans Excel en français.

UTILISATION (Windows, invite de commandes)
  python inventaire_images.py "D:\\FREDD" "E:\\Archives_2023"
  python inventaire_images.py "D:\\FREDD" --sortie "D:\\Rapports"
  python inventaire_images.py "D:\\FREDD" --reference-csv "D:\\FREDD\\data_clean.csv"

Voir `python inventaire_images.py --help` pour toutes les options.
"""

import argparse
import csv
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path

# Force l'UTF-8 sur les terminaux Windows (cp1252 par défaut), pour afficher
# correctement les accents dans la console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DICOM_EXT = ".dcm"
E2E_EXT = ".e2e"
ZIP_EXT = ".zip"

DEFAULT_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".heif"}

# Archives rencontrées mais non prises en charge : on les signale clairement
# dans le rapport d'erreurs plutôt que de les ignorer silencieusement.
UNSUPPORTED_ARCHIVE_EXTS = {".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".arj", ".cab", ".iso"}

# Noms de dossiers "techniques" à ignorer pour la détection de l'identifiant
# patient (repris de analyze.py).
NON_ID_FOLDER_NAMES = {"OD", "OS", "OCT", "FAF", "CLARUS", "OPTOS", "CF", "GC", "MAC"}

PROGRESS_EVERY = 2000

INVENTORY_HEADERS = [
    "chemin", "dans_archive", "archive_racine", "profondeur_zip",
    "racine_scan", "lot", "nom_fichier", "extension", "categorie",
    "format_detecte", "verification_entete", "taille_octets", "taille_lisible",
    "date_modification", "patient_id_detecte", "id_connu_reference", "sha256",
]

ERROR_HEADERS = ["horodatage", "chemin", "racine_scan", "lot", "contexte", "type_erreur", "message"]

LOT_HEADERS = [
    "racine_scan", "lot", "nb_patients_distincts", "nb_images_total",
    "nb_images_dicom", "nb_images_e2e", "nb_images_standard",
    "nb_fichiers_en_erreur",
]

SYNTHESE_HEADERS = ["indicateur", "valeur", "explication"]

MULTI_LOT_HEADERS = [
    "patient_id_detecte", "nb_lots", "liste_lots", "nb_images_total_patient",
    "nb_images_uniques_contenu", "nb_images_dupliquees_entre_lots",
    "statut_doublons",
    "STATUT",
    "detail_par_lot",
]


_UUID_RE = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.I)
_HEX_ID_RE = re.compile(r'\b([0-9a-f]{6,12})\b', re.I)


def extract_id(text):
    """Extrait un identifiant patient (UUID complet ou hexadécimal court) d'une chaîne."""
    if not text:
        return None
    m = _UUID_RE.search(text)
    if m:
        return m.group(1).lower()
    for m in _HEX_ID_RE.finditer(text):
        val = m.group(1).lower()
        if val.isdigit() and len(val) == 8:
            # On évite de confondre un identifiant avec une date AAAAMMJJ.
            year, month, day = int(val[:4]), int(val[4:6]), int(val[6:8])
            if 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                continue
        return val
    return None


def canonicalize_patient_id(pid):
    if not pid:
        return None
    cleaned = pid.replace("-", "").lower()
    if re.fullmatch(r"[0-9a-f]{6,}", cleaned):
        return cleaned[:8] if len(cleaned) >= 8 else cleaned
    return cleaned


def detect_patient_id(display_path):
    """Heuristique best-effort : cherche d'abord un ID dans les noms de dossiers
    parents (en ignorant les dossiers techniques OD/OS/OCT/...), puis dans le
    nom de fichier. Ne garantit pas l'exactitude : c'est une aide au tri, pas
    une identité certifiée."""
    norm = display_path.replace("::", "/").replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return ""
    filename = parts[-1]
    stem = os.path.splitext(filename)[0]
    for part in reversed(parts[:-1]):
        if part.upper() in NON_ID_FOLDER_NAMES:
            continue
        found = extract_id(part)
        if found:
            return canonicalize_patient_id(found)
    found = extract_id(stem)
    return canonicalize_patient_id(found) if found else ""


CSV_ID_HEADERS = {"id", "patient_id", "adm_identifiant_bamara"}


def load_reference_ids(csv_path: Path):
    ids = set()
    if not csv_path or not csv_path.exists():
        return ids
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            sample = f.read(2048)
            f.seek(0)
            delimiter = ";" if sample.count(";") >= sample.count(",") else ","
            reader = csv.reader(f, delimiter=delimiter)
            header = [h.strip().strip('"').lower() for h in next(reader, [])]
            id_cols = [i for i, h in enumerate(header) if h in CSV_ID_HEADERS]
            if not id_cols:
                return ids
            for row in reader:
                for i in id_cols:
                    if i < len(row):
                        val = extract_id(row[i].strip())
                        if val:
                            canon = canonicalize_patient_id(val)
                            ids.add(canon)
                            ids.add(val)
    except Exception as e:
        print(f"[ATTENTION] Impossible de lire le CSV de référence {csv_path}: {e}")
    return ids


def sniff_image_format(header: bytes):
    """Devine le format d'une image à partir de ses premiers octets, sans la
    décoder (pas d'ouverture PIL, pas de lecture des pixels)."""
    if not header:
        return None
    if header[:3] == b"\xFF\xD8\xFF":
        return "JPEG"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if header[:2] == b"BM":
        return "BMP"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "WEBP"
    if header[4:8] == b"ftyp" and header[8:12] in (
        b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis", b"avif", b"avis"
    ):
        return "HEIC/HEIF"
    return None


def is_dicom_header(header: bytes):
    return bool(header) and len(header) >= 132 and header[128:132] == b"DICM"


def classify(ext, header, extra_image_exts):
    """Retourne (categorie, format_detecte, verification) ou None si le
    fichier n'est pas un type d'image que l'on inventorie."""
    ext = ext.lower()
    image_exts = DEFAULT_IMAGE_EXTS | extra_image_exts

    if ext == DICOM_EXT:
        ok = is_dicom_header(header)
        return ("DICOM", "DICOM", "OK" if ok else "ECHEC_ENTETE")

    if ext == E2E_EXT:
        # Format propriétaire Heidelberg sans signature publique fiable :
        # on se fie à l'extension, comme le fait le code existant (analyze.py).
        return ("E2E_HEIDELBERG", "E2E", "NON_VERIFIE")

    if ext in image_exts:
        fmt = sniff_image_format(header)
        if fmt:
            return ("IMAGE_STANDARD", fmt, "OK")
        return ("IMAGE_STANDARD", ext.lstrip(".").upper(), "ECHEC_ENTETE")

    if ext == "":
        if is_dicom_header(header):
            return ("DICOM", "DICOM (sans extension)", "OK")
        fmt = sniff_image_format(header)
        if fmt:
            return ("IMAGE_STANDARD", f"{fmt} (sans extension)", "OK")
        return None

    return None


def format_taille(n):
    if n is None:
        return ""
    n = float(n)
    for unit in ["o", "Ko", "Mo", "Go", "To"]:
        if n < 1024 or unit == "To":
            return f"{n:.2f} {unit}"
        n /= 1024


def long_path_str(p):
    """Sur Windows, préfixe les chemins absolus avec \\\\?\\ pour contourner la
    limite historique de 260 caractères (MAX_PATH), utile pour les
    arborescences très profondes. Sans effet sur les autres OS."""
    s = os.path.abspath(str(p))
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def clean_display(s):
    """Retire le préfixe \\\\?\\ pour l'affichage / les rapports CSV."""
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[len("\\\\?\\UNC\\"):]
    if s.startswith("\\\\?\\"):
        return s[len("\\\\?\\"):]
    return s


class Contexte:
    """Regroupe l'état partagé pendant le scan : writers CSV, compteurs,
    et les structures utilisées pour le bilan par lot."""

    def __init__(self, args, out_writer, err_writer, known_ids):
        self.args = args
        self.out_writer = out_writer
        self.err_writer = err_writer
        self.known_ids = known_ids
        self.extra_image_exts = set(
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in args.extensions_image_suppl.split(",") if e.strip()
        )
        self.compteurs = Counter()
        # lot_stats[(racine, lot)] -> Counter(categorie -> n), + set patients
        self.lot_categorie = defaultdict(Counter)
        self.lot_patients = defaultdict(set)
        self.lot_erreurs = Counter()
        # patient_id -> lot -> list of (sha256, chemin)
        self.patient_lot_fichiers = defaultdict(lambda: defaultdict(list))
        # Vue globale dédupliquée, tous lots confondus : nécessaire car
        # lot_patients/lot_categorie sont volontairement comptés PAR LOT (un
        # patient présent dans 3 lots contribue à 3 lignes de bilan_par_lot.csv).
        # Sans ceci, additionner nb_patients_distincts entre lots surcompte
        # tout patient (ou toute image identique) partagé entre plusieurs lots.
        self.tous_patients = set()
        self.tous_hashes = set()
        # Option --analyse-contenu : writer du CSV contenu_images (None = désactivé)
        self.contenu_writer = None
        self.contenu_modalites = Counter()

    def log_error(self, chemin, racine, lot, contexte, type_erreur, message):
        self.compteurs["erreurs"] += 1
        self.lot_erreurs[(racine, lot)] += 1
        self.err_writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            chemin, racine, lot, contexte, type_erreur, message,
        ])
        print(f"  [ERREUR] {contexte} :: {chemin} :: {type_erreur}: {message}")

    def write_row(self, chemin, dans_archive, archive_racine, profondeur, racine, lot,
                  nom_fichier, ext, categorie, format_detecte, verif, taille, date_mod,
                  patient_id, sha256_val):
        id_connu = ""
        if self.known_ids:
            id_connu = "Oui" if (patient_id and patient_id in self.known_ids) else "Non"
        self.out_writer.writerow([
            chemin, "Oui" if dans_archive else "Non", archive_racine, profondeur,
            racine, lot, nom_fichier, ext, categorie, format_detecte, verif,
            taille, format_taille(taille), date_mod, patient_id, id_connu, sha256_val,
        ])
        self.compteurs["images_total"] += 1
        if categorie == "DICOM":
            self.compteurs["dicom"] += 1
        elif categorie == "E2E_HEIDELBERG":
            self.compteurs["e2e"] += 1
        elif categorie == "IMAGE_STANDARD":
            self.compteurs["standard"] += 1
        self.lot_categorie[(racine, lot)][categorie] += 1
        if patient_id:
            self.lot_patients[(racine, lot)].add(patient_id)
            self.tous_patients.add(patient_id)
            # On enregistre le fichier même sans empreinte (sha256_val peut être vide si
            # --pas-de-hash ou en cas d'erreur de calcul) : la présence multi-lots doit
            # rester détectée dans tous les cas, seule la comparaison fine des doublons
            # en dépend (voir build_multi_lot_rows).
            self.patient_lot_fichiers[patient_id][lot].append((sha256_val or None, chemin))
        if sha256_val:
            self.tous_hashes.add(sha256_val)


def sha256_of_bytes_stream(read_func, chunk_size=1024 * 1024):
    import hashlib
    h = hashlib.sha256()
    while True:
        chunk = read_func(chunk_size)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def handle_fs_file(fpath_access, fpath_display, ext, racine_display, lot, ctx):
    args = ctx.args
    need_header = ext == DICOM_EXT or ext in (DEFAULT_IMAGE_EXTS | ctx.extra_image_exts) or ext == ""
    if not need_header and ext != E2E_EXT:
        return  # fichier hors périmètre, on ignore silencieusement

    header = None
    if need_header:
        try:
            with open(fpath_access, "rb") as f:
                header = f.read(132)
        except Exception as e:
            ctx.log_error(fpath_display, racine_display, lot, "lecture en-tête", type(e).__name__, str(e))
            return

    result = classify(ext, header, ctx.extra_image_exts)
    if result is None:
        return
    categorie, format_detecte, verif = result

    try:
        st = os.stat(fpath_access)
        taille = st.st_size
        date_mod = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except Exception as e:
        ctx.log_error(fpath_display, racine_display, lot, "lecture métadonnées", type(e).__name__, str(e))
        return

    sha256_val = ""
    if not args.pas_de_hash:
        try:
            with open(fpath_access, "rb") as f:
                sha256_val = sha256_of_bytes_stream(f.read)
        except Exception as e:
            ctx.log_error(fpath_display, racine_display, lot, "calcul hash", type(e).__name__, str(e))

    patient_id = detect_patient_id(fpath_display)
    nom_fichier = os.path.basename(fpath_display)
    ctx.write_row(fpath_display, False, "", 0, racine_display, lot, nom_fichier, ext,
                  categorie, format_detecte, verif, taille, date_mod, patient_id, sha256_val)
    analyser_contenu(ctx, fpath_display, racine_display, lot, patient_id, categorie, chemin_acces=fpath_access)


CONTENU_HEADERS = ["chemin", "lot", "patient_id_detecte", "categorie", "serie", "modalite", "lateralite",
                   "source_oeil", "orientation", "nb_bscans", "motif_scan", "appareil", "alerte", "detail", "erreur"]


def analyser_contenu(ctx, chemin_affiche, racine, lot, patient_id, categorie, chemin_acces=None, zf=None, info=None):
    """Décrit le contenu d'un DICOM / E2E (voir analyse_contenu.py) sans
    décoder les pixels ni lire l'identité patient. Les entrées de zip sont
    extraites dans un fichier temporaire supprimé aussitôt."""
    if ctx.contenu_writer is None or categorie not in ("DICOM", "E2E_HEIDELBERG"):
        return
    import analyse_contenu  # import tardif : l'inventaire seul reste sans dépendance
    tmp_path = None
    try:
        if zf is not None:
            suffixe = ".e2e" if categorie == "E2E_HEIDELBERG" else ".dcm"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffixe) as tmp, zf.open(info) as src:
                tmp_path = tmp.name
                shutil.copyfileobj(src, tmp, length=1024 * 1024)
            chemin_acces = tmp_path
        lignes = analyse_contenu.analyser(chemin_acces, categorie, chemin_affiche)
    except Exception as e:  # noqa: BLE001
        ctx.log_error(chemin_affiche, racine, lot, "analyse contenu", type(e).__name__, str(e))
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    for l in lignes:
        ctx.contenu_writer.writerow([chemin_affiche, lot, patient_id, categorie] +
                                    [l.get(k, "") for k in CONTENU_HEADERS[4:]])
        ctx.contenu_modalites["illisible" if l.get("erreur") else l.get("modalite", "?")] += 1


def handle_zip_entry(zf, info, entry_display, ext, racine_display, lot, archive_racine_display,
                      profondeur, ctx):
    args = ctx.args
    need_header = ext == DICOM_EXT or ext in (DEFAULT_IMAGE_EXTS | ctx.extra_image_exts) or ext == ""
    if not need_header and ext != E2E_EXT:
        return

    header = None
    if need_header:
        try:
            with zf.open(info) as f:
                header = f.read(132)
        except Exception as e:
            ctx.log_error(entry_display, racine_display, lot, "lecture en-tête (zip)", type(e).__name__, str(e))
            return

    result = classify(ext, header, ctx.extra_image_exts)
    if result is None:
        return
    categorie, format_detecte, verif = result

    taille = info.file_size
    try:
        date_mod = datetime(*info.date_time).isoformat(timespec="seconds")
    except Exception:
        date_mod = ""

    sha256_val = ""
    if not args.pas_de_hash:
        try:
            with zf.open(info) as f:
                sha256_val = sha256_of_bytes_stream(f.read)
        except Exception as e:
            ctx.log_error(entry_display, racine_display, lot, "calcul hash (zip)", type(e).__name__, str(e))

    patient_id = detect_patient_id(entry_display)
    nom_fichier = os.path.basename(info.filename)
    ctx.write_row(entry_display, True, archive_racine_display, profondeur, racine_display, lot,
                  nom_fichier, ext, categorie, format_detecte, verif, taille, date_mod,
                  patient_id, sha256_val)
    analyser_contenu(ctx, entry_display, racine_display, lot, patient_id, categorie, zf=zf, info=info)


def process_zip(path_for_zipfile, display_path, racine_display, lot, archive_racine_display,
                 profondeur, ctx):
    args = ctx.args
    if profondeur > args.profondeur_zip_max:
        ctx.log_error(display_path, racine_display, lot, "profondeur zip",
                       "ProfondeurZipDepassee",
                       f"Profondeur d'imbrication > {args.profondeur_zip_max}, archive ignorée.")
        return

    try:
        zf = zipfile.ZipFile(path_for_zipfile)
    except zipfile.BadZipFile as e:
        ctx.log_error(display_path, racine_display, lot, "ouverture zip", "BadZipFile",
                       f"Archive corrompue, chiffrée ou dans un format propriétaire non standard : {e}")
        return
    except NotImplementedError as e:
        ctx.log_error(display_path, racine_display, lot, "ouverture zip", "CompressionNonSupportee", str(e))
        return
    except RuntimeError as e:
        ctx.log_error(display_path, racine_display, lot, "ouverture zip", "ZipProtegeOuInvalide", str(e))
        return
    except OSError as e:
        ctx.log_error(display_path, racine_display, lot, "ouverture zip", type(e).__name__, str(e))
        return

    with zf:
        ctx.compteurs["zips_ouverts"] += 1
        for info in zf.infolist():
            if info.is_dir():
                continue
            entry_display = f"{display_path} :: {info.filename}"
            ctx.compteurs["fichiers_vus"] += 1
            if ctx.compteurs["fichiers_vus"] % PROGRESS_EVERY == 0:
                print(f"... {ctx.compteurs['fichiers_vus']} fichiers examinés "
                      f"({ctx.compteurs['images_total']} images trouvées, {ctx.compteurs['erreurs']} erreurs)")
            entry_ext = os.path.splitext(info.filename)[1].lower()
            try:
                if entry_ext == ZIP_EXT:
                    ctx.compteurs["zips_imbriques"] += 1
                    handle_nested_zip(zf, info, entry_display, racine_display, lot,
                                       archive_racine_display, profondeur, ctx)
                elif entry_ext in UNSUPPORTED_ARCHIVE_EXTS:
                    ctx.log_error(entry_display, racine_display, lot, "archive non prise en charge (dans zip)",
                                   "TypeArchiveNonSupporte",
                                   f"Extension {entry_ext} non gérée par cet inventaire (zip uniquement).")
                else:
                    handle_zip_entry(zf, info, entry_display, entry_ext, racine_display, lot,
                                      archive_racine_display, profondeur, ctx)
            except Exception as e:
                ctx.log_error(entry_display, racine_display, lot, "lecture entrée zip", type(e).__name__, str(e))


def handle_nested_zip(zf, info, entry_display, racine_display, lot, archive_racine_display,
                       profondeur, ctx):
    args = ctx.args
    seuil_octets = args.taille_max_zip_memoire_mo * 1024 * 1024
    if info.file_size <= seuil_octets:
        try:
            data = zf.read(info)
        except Exception as e:
            ctx.log_error(entry_display, racine_display, lot, "extraction zip imbriqué (mémoire)",
                           type(e).__name__, str(e))
            return
        process_zip(io.BytesIO(data), entry_display, racine_display, lot,
                    archive_racine_display, profondeur + 1, ctx)
    else:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
                tmp_path = tmp.name
                with zf.open(info) as src:
                    shutil.copyfileobj(src, tmp, length=1024 * 1024)
            process_zip(tmp_path, entry_display, racine_display, lot,
                        archive_racine_display, profondeur + 1, ctx)
        except Exception as e:
            ctx.log_error(entry_display, racine_display, lot, "extraction zip imbriqué (temporaire)",
                           type(e).__name__, str(e))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


def compute_lot(racine_display, fpath_display):
    """Le "lot" est le sous-dossier de premier niveau sous la racine scannée
    (ex: racine/lot12/...), préfixé par le nom de la racine pour éviter les
    ambiguïtés si plusieurs racines sont scannées en une seule fois.

    Cas particulier fréquent dans ce projet : un lot n'est pas toujours un
    dossier, c'est parfois directement une archive .zip posée à la racine
    (ex: racine/lot16.zip). Dans ce cas, le lot prend le nom de l'archive
    elle-même (sans l'extension .zip), pas "(racine)"."""
    racine_name = Path(racine_display).name or str(racine_display)
    try:
        rel = os.path.relpath(fpath_display, racine_display)
    except ValueError:
        rel = os.path.basename(fpath_display)
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if len(parts) >= 2:
        return f"{racine_name}/{parts[0]}"
    if len(parts) == 1:
        stem, ext = os.path.splitext(parts[0])
        if ext.lower() == ".zip":
            # Suffixe "(zip)" systématique : évite qu'un dossier "lot1" et une
            # archive "lot1.zip" ne soient (silencieusement) comptés comme un
            # seul et même lot si les deux existent en parallèle sur le disque
            # (cas fréquent : l'archive d'origine gardée après extraction).
            # Les deux restent ainsi deux lots distincts et visibles dans le
            # bilan, et un patient dupliqué entre les deux ressort clairement
            # dans patients_multi_lots.csv au lieu d'être noyé dans un seul total.
            return f"{racine_name}/{stem} (zip)"
    return f"{racine_name}/(racine)"


def scan_root(racine_display, ctx):
    racine_display = str(Path(racine_display).resolve())
    access_root = long_path_str(racine_display)
    print(f"\n=== Analyse du dossier : {racine_display} ===")

    def onerror(err):
        ctx.log_error(clean_display(getattr(err, "filename", racine_display)), racine_display,
                       f"{Path(racine_display).name}/(racine)", "listage dossier",
                       type(err).__name__, str(err))

    # Liens symboliques de dossiers (ex. stockage monté par lien dans un environnement Jupyter) :
    # suivis, sinon leur contenu serait ignoré sans bruit. Chaque dossier réel n'est parcouru qu'une
    # fois (protection contre les boucles et les doubles chemins), et le lien ignoré est tracé.
    deja_parcourus = set()
    for current_root, dirs, files in os.walk(access_root, onerror=onerror, followlinks=True):
        reel = os.path.normcase(os.path.realpath(current_root))
        if reel in deja_parcourus:
            dirs[:] = []
            ctx.log_error(clean_display(current_root), racine_display, f"{Path(racine_display).name}/(racine)",
                          "listage dossier", "LienSymboliqueDejaParcouru",
                          f"Dossier déjà parcouru via un autre chemin ({reel}) : ignoré pour éviter doublons et boucles.")
            continue
        deja_parcourus.add(reel)
        current_root_display = clean_display(current_root)
        for fname in files:
            ctx.compteurs["fichiers_vus"] += 1
            if ctx.compteurs["fichiers_vus"] % PROGRESS_EVERY == 0:
                print(f"... {ctx.compteurs['fichiers_vus']} fichiers examinés "
                      f"({ctx.compteurs['images_total']} images trouvées, {ctx.compteurs['erreurs']} erreurs) "
                      f"— dossier courant : {current_root_display}")

            fpath_access = os.path.join(current_root, fname)
            fpath_display = os.path.join(current_root_display, fname)
            lot = compute_lot(racine_display, fpath_display)
            ext = os.path.splitext(fname)[1].lower()

            try:
                if ext == ZIP_EXT:
                    ctx.compteurs["zips_trouves"] += 1
                    print(f"  [ZIP] {fpath_display}")
                    if ctx.args.pas_de_zip:
                        ctx.log_error(fpath_display, racine_display, lot, "archive zip",
                                       "ZipNonAnalyse",
                                       "Analyse des zip désactivée (--pas-de-zip) : contenu non inventorié.")
                    else:
                        process_zip(fpath_access, fpath_display, racine_display, lot,
                                    fpath_display, 0, ctx)
                elif ext in UNSUPPORTED_ARCHIVE_EXTS:
                    ctx.log_error(fpath_display, racine_display, lot, "archive non prise en charge",
                                   "TypeArchiveNonSupporte",
                                   f"Extension {ext} non gérée par cet inventaire (zip uniquement).")
                else:
                    handle_fs_file(fpath_access, fpath_display, ext, racine_display, lot, ctx)
            except PermissionError as e:
                ctx.log_error(fpath_display, racine_display, lot, "accès fichier", "PermissionError", str(e))
            except OSError as e:
                ctx.log_error(fpath_display, racine_display, lot, "accès fichier", type(e).__name__, str(e))
            except Exception as e:
                ctx.log_error(fpath_display, racine_display, lot, "erreur inattendue", type(e).__name__, str(e))


def write_lot_report(lot_path: Path, ctx):
    with open(lot_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(LOT_HEADERS)
        for (racine, lot), cats in sorted(ctx.lot_categorie.items()):
            nb_patients = len(ctx.lot_patients.get((racine, lot), set()))
            nb_total = sum(cats.values())
            w.writerow([
                racine, lot, nb_patients, nb_total,
                cats.get("DICOM", 0), cats.get("E2E_HEIDELBERG", 0), cats.get("IMAGE_STANDARD", 0),
                ctx.lot_erreurs.get((racine, lot), 0),
            ])
    return lot_path


def build_multi_lot_rows(ctx):
    """Pour chaque patient présent dans >= 2 lots, détermine si les images
    sont identiques (mêmes fichiers, même contenu), différentes, ou un mélange
    des deux, en comparant les empreintes SHA-256 des fichiers entre lots."""
    rows = []
    for patient_id, par_lot in ctx.patient_lot_fichiers.items():
        lots = sorted(par_lot.keys())
        if len(lots) < 2:
            continue
        total_images = sum(len(fichiers) for fichiers in par_lot.values())

        # hash -> ensemble des lots dans lesquels ce hash apparaît (fichiers sans
        # empreinte calculée exclus de cette comparaison, mais comptés à part).
        hash_to_lots = defaultdict(set)
        nb_sans_hash = 0
        for lot, fichiers in par_lot.items():
            for h, _chemin in fichiers:
                if h is None:
                    nb_sans_hash += 1
                else:
                    hash_to_lots[h].add(lot)

        hash_partages = {h for h, lts in hash_to_lots.items() if len(lts) > 1}
        nb_hash_uniques = len(hash_to_lots)
        nb_images_dupliquees = sum(
            len(fichiers) for lot, fichiers in par_lot.items()
            for h, _c in fichiers if h in hash_partages
        )

        if nb_hash_uniques == 0:
            statut = "INDETERMINE (empreintes non calculées, relancer sans --pas-de-hash)"
        elif hash_partages and len(hash_partages) == nb_hash_uniques and nb_sans_hash == 0:
            statut = "IMAGES IDENTIQUES (copies exactes dans tous les lots)"
        elif hash_partages:
            statut = "PARTIEL (certaines images communes, d'autres propres à un lot)"
        else:
            statut = "IMAGES DIFFERENTES (aucun fichier identique entre les lots)"
        if nb_sans_hash:
            statut += f" [{nb_sans_hash} fichier(s) sans empreinte calculée]"

        detail = " | ".join(f"{lot}: {len(fichiers)} fichier(s)" for lot, fichiers in sorted(par_lot.items()))

        rows.append([
            patient_id, len(lots), ", ".join(lots), total_images,
            nb_hash_uniques, nb_images_dupliquees, statut,
            "MULTI-LOT",
            detail,
        ])
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def write_multi_lot_report(path: Path, ctx):
    rows = build_multi_lot_rows(ctx)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(MULTI_LOT_HEADERS)
        for r in rows:
            w.writerow(r)
    return path, rows


def write_synthese_globale(path: Path, ctx, multi_rows):
    """bilan_par_lot.csv compte volontairement CHAQUE lot séparément : un
    patient présent dans 3 lots contribue à 3 lignes (par construction, un
    bilan "par lot" ne peut pas faire autrement). Ce rapport donne en plus les
    totaux réels, dédupliqués sur l'ensemble du scan, pour ne pas avoir à
    additionner soi-même une colonne qui compterait certains patients/images
    plusieurs fois."""
    somme_patients_par_lot = sum(len(s) for s in ctx.lot_patients.values())
    nb_patients_uniques = len(ctx.tous_patients)
    nb_images_total = ctx.compteurs["images_total"]
    nb_images_uniques = len(ctx.tous_hashes) if not ctx.args.pas_de_hash else None

    rows = [
        ("Nombre de lots détectés", len(ctx.lot_categorie),
         "Un lot = un sous-dossier de premier niveau sous la racine scannée, ou une archive .zip "
         "posée directement à la racine (suffixée \"(zip)\")."),
        ("Somme des \"nb_patients_distincts\" de bilan_par_lot.csv", somme_patients_par_lot,
         "ATTENTION : compte un même patient une fois PAR LOT où il apparaît. Ne pas utiliser "
         "comme total réel de patients si des patients sont présents dans plusieurs lots (voir "
         "la ligne suivante, et patients_multi_lots.csv pour le détail)."),
        ("Nombre de patients uniques, tous lots confondus (dédupliqué)", nb_patients_uniques,
         "Le vrai total de patients distincts sur l'ensemble du scan, quel que soit le nombre de "
         "lots dans lesquels chacun apparaît. C'est ce chiffre qu'il faut citer comme \"nombre de "
         "patients\" du projet, pas la somme par lot ci-dessus."),
        ("Patients présents dans plus d'un lot", len(multi_rows),
         "Détail et statut des doublons d'images (identiques / différentes / partiel) dans "
         "patients_multi_lots.csv."),
        ("Nombre total d'images inventoriées (toutes occurrences)", nb_images_total,
         "Chaque fichier trouvé compte une fois, même s'il s'agit d'une copie identique d'un "
         "fichier déjà comptabilisé dans un autre lot (ex: une archive .zip jamais supprimée "
         "après extraction d'un dossier)."),
        ("Nombre d'images de contenu unique, tous lots confondus (dédupliqué par SHA-256)",
         nb_images_uniques if nb_images_uniques is not None else "N/D (relancer sans --pas-de-hash)",
         "Nombre réel de fichiers-images physiquement distincts par leur contenu binaire, tous "
         "lots et tous patients confondus (inclut aussi les doublons non rattachés à un patient "
         "identifié)."),
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(SYNTHESE_HEADERS)
        for indicateur, valeur, explication in rows:
            w.writerow([indicateur, valeur, explication])
    return path, {
        "nb_patients_uniques": nb_patients_uniques,
        "somme_patients_par_lot": somme_patients_par_lot,
        "nb_images_uniques": nb_images_uniques,
        "nb_images_total": nb_images_total,
    }


def parse_args():
    p = argparse.ArgumentParser(
        description="Inventaire des images FREDD (DICOM/.e2e/images standards), y compris dans des zip imbriqués.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("dossiers", nargs="+", help="Un ou plusieurs dossiers racines à inventorier.")
    p.add_argument("--sortie", type=Path, default=Path.cwd(),
                   help="Dossier où écrire les CSV de résultats (par défaut : dossier courant).")
    p.add_argument("--reference-csv", type=Path, default=None,
                   help="CSV optionnel (ex: data_clean.csv) avec une colonne 'id'/'patient_id' "
                        "pour indiquer si chaque identifiant détecté est déjà connu.")
    p.add_argument("--extensions-image-suppl", default="",
                   help="Extensions d'image supplémentaires à inclure, séparées par des virgules (ex: .cr2,.nef).")
    p.add_argument("--profondeur-zip-max", type=int, default=10,
                   help="Profondeur maximale d'imbrication de zip dans zip (défaut: 10).")
    p.add_argument("--taille-max-zip-memoire-mo", type=int, default=200,
                   help="Au-delà de cette taille (Mo), un zip imbriqué est extrait dans un fichier "
                        "temporaire plutôt qu'en mémoire (défaut: 200).")
    p.add_argument("--pas-de-zip", action="store_true",
                   help="Ne pas ouvrir les fichiers .zip (les liste seulement comme non analysés).")
    p.add_argument("--pas-de-hash", action="store_true",
                   help="Désactive le calcul d'empreinte SHA-256 des fichiers : plus rapide, mais "
                        "empêche la détection des doublons d'images entre lots pour un même patient.")
    p.add_argument("--analyse-contenu", action="store_true",
                   help="Décrit aussi le CONTENU des DICOM et .e2e (modalité OCT / AF / fond d'œil…, œil, "
                        "orientation des coupes OCT, nombre de B-scans) dans contenu_images_<horodatage>.csv. "
                        "Nécessite analyse_contenu.py (même dossier) + pydicom et oct-converter.")
    return p.parse_args()


def main():
    args = parse_args()
    args.sortie.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    inv_path = args.sortie / f"inventaire_images_{ts}.csv"
    err_path = args.sortie / f"erreurs_inventaire_{ts}.csv"
    lot_path = args.sortie / f"bilan_par_lot_{ts}.csv"
    multi_path = args.sortie / f"patients_multi_lots_{ts}.csv"
    synthese_path = args.sortie / f"synthese_globale_{ts}.csv"

    known_ids = set()
    if args.reference_csv:
        known_ids = load_reference_ids(args.reference_csv)
        print(f"IDs de référence chargés depuis {args.reference_csv} : {len(known_ids)}")

    print(f"Résultats écrits dans : {args.sortie.resolve()}")
    if not args.pas_de_hash:
        print("Calcul des empreintes SHA-256 activé (nécessaire pour le bilan de doublons inter-lots).")
    else:
        print("Calcul des empreintes SHA-256 désactivé (--pas-de-hash) : le bilan de doublons "
              "inter-lots ne pourra pas être établi.")

    with open(inv_path, "w", newline="", encoding="utf-8-sig") as f_inv, \
         open(err_path, "w", newline="", encoding="utf-8-sig") as f_err:

        out_writer = csv.writer(f_inv, delimiter=";")
        out_writer.writerow(INVENTORY_HEADERS)
        err_writer = csv.writer(f_err, delimiter=";")
        err_writer.writerow(ERROR_HEADERS)

        ctx = Contexte(args, out_writer, err_writer, known_ids)
        f_contenu = None
        if args.analyse_contenu:
            contenu_path = args.sortie / f"contenu_images_{ts}.csv"
            f_contenu = open(contenu_path, "w", newline="", encoding="utf-8-sig")
            ctx.contenu_writer = csv.writer(f_contenu, delimiter=";")
            ctx.contenu_writer.writerow(CONTENU_HEADERS)
            print("Analyse du contenu activée (modalité, œil, orientation OCT) : plus lent.")

        try:
            for dossier in args.dossiers:
                dpath = Path(dossier)
                if not dpath.exists():
                    ctx.log_error(str(dpath), str(dpath), "(racine)", "dossier racine",
                                   "DossierIntrouvable", "Le dossier indiqué n'existe pas.")
                    continue
                scan_root(dpath, ctx)
        except KeyboardInterrupt:
            print("\nInterrompu par l'utilisateur : les résultats partiels sont conservés.")
        finally:
            if f_contenu is not None:
                f_contenu.close()

    if f_contenu is not None:
        print("\n  Contenu détecté (une ligne par série d'images) : "
              + ", ".join(f"{k}={v}" for k, v in sorted(ctx.contenu_modalites.items())))
        print(f"    -> {contenu_path}")

    lot_path = write_lot_report(lot_path, ctx)
    multi_path, multi_rows = write_multi_lot_report(multi_path, ctx)
    synthese_path, synthese = write_synthese_globale(synthese_path, ctx, multi_rows)

    c = ctx.compteurs
    print("\n" + "=" * 70)
    print("  RÉSUMÉ")
    print("=" * 70)
    print(f"  Fichiers examinés (disque + contenu des zip) : {c['fichiers_vus']}")
    print(f"  Images trouvées au total                     : {c['images_total']}")
    print(f"    - DICOM (.dcm)                              : {c['dicom']}")
    print(f"    - Heidelberg (.e2e)                          : {c['e2e']}")
    print(f"    - Images standards (jpg/png/tiff/...)        : {c['standard']}")
    print(f"  Archives .zip ouvertes                        : {c['zips_ouverts']} "
          f"(dont {c['zips_imbriques']} imbriquées)")
    print(f"  Erreurs / fichiers non récupérés              : {c['erreurs']}")

    print("\n  Bilan par lot (sous-dossier de premier niveau) :")
    for (racine, lot), cats in sorted(ctx.lot_categorie.items()):
        nb_patients = len(ctx.lot_patients.get((racine, lot), set()))
        print(f"    - {lot:<40} patients={nb_patients:<5} images={sum(cats.values()):<6} "
              f"erreurs={ctx.lot_erreurs.get((racine, lot), 0)}")

    print("\n  ⚠ Totaux dédupliqués (tous lots confondus) — à utiliser pour un vrai total projet :")
    print(f"    - Somme naïve des patients par lot (bilan_par_lot.csv)   : {synthese['somme_patients_par_lot']} "
          f"{'(= surcompte, un même patient présent dans N lots est compté N fois)' if synthese['somme_patients_par_lot'] != synthese['nb_patients_uniques'] else '(aucun patient partagé entre lots ici)'}")
    print(f"    - Nombre RÉEL de patients uniques                        : {synthese['nb_patients_uniques']}")
    print(f"    - Nombre total d'images inventoriées (avec doublons)     : {synthese['nb_images_total']}")
    if synthese["nb_images_uniques"] is not None:
        print(f"    - Nombre RÉEL d'images de contenu unique (SHA-256)       : {synthese['nb_images_uniques']}")
    else:
        print(f"    - Nombre RÉEL d'images de contenu unique                 : indisponible (--pas-de-hash actif)")

    if multi_rows:
        print(f"\n  Patients présents dans plusieurs lots : {len(multi_rows)}")
        for r in multi_rows[:20]:
            patient_id, nb_lots, liste_lots, total_img, nb_uniq, nb_dup, statut, _flag, _detail = r
            print(f"    - {patient_id:<12} ({nb_lots} lots: {liste_lots}) -> {statut}")
        if len(multi_rows) > 20:
            print(f"    ... et {len(multi_rows) - 20} autre(s) (voir {multi_path.name})")
    else:
        print("\n  Aucun patient détecté dans plusieurs lots à la fois.")

    print(f"\n  Fichiers générés :")
    print(f"    - Inventaire complet      : {inv_path}")
    print(f"    - Erreurs / non récupérés : {err_path}")
    print(f"    - Bilan par lot           : {lot_path}")
    print(f"    - Patients multi-lots     : {multi_path}")
    print(f"    - Synthèse globale (dédupliquée) : {synthese_path}")


if __name__ == "__main__":
    main()
