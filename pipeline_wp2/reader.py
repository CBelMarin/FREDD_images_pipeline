"""Lecture des fichiers DICOM et E2E, extraction des images et métadonnées."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pydicom

logger = logging.getLogger(__name__)


@dataclass
class ImageData:
    """Conteneur pour une image extraite avec ses métadonnées."""
    image: np.ndarray
    metadata: dict = field(default_factory=dict)
    source_file: str = ""
    image_index: int = 0
    image_type: str = "unknown"  # fundus_color, fundus_grayscale, oct_bscan, autofluorescence


def read_file(file_path: str | Path) -> list[ImageData]:
    """Lit un fichier DICOM ou E2E et retourne la liste des images extraites."""
    file_path = Path(file_path)
    suffix = file_path.suffix.upper()

    if suffix == ".DCM" or suffix == ".DICOM":
        return read_dicom(file_path)
    elif suffix == ".E2E":
        return read_e2e(file_path)
    else:
        logger.warning("Format non reconnu: %s", file_path)
        return []


# ---------------------------------------------------------------------------
# DICOM
# ---------------------------------------------------------------------------

def read_dicom(file_path: Path) -> list[ImageData]:
    """Lit un fichier DICOM ophtalmique et extrait les images exploitables."""
    try:
        ds = pydicom.dcmread(str(file_path))
    except Exception as e:
        logger.error("Impossible de lire %s: %s", file_path.name, e)
        return []

    # Ignorer les fichiers Raw Data et les fichiers d'élaboration (pas de pixels standard)
    sop_class = str(getattr(ds, "SOPClassUID", ""))
    if sop_class == "1.2.840.10008.5.1.4.1.1.66":  # Raw Data Storage
        logger.info("Fichier Raw Data ignoré: %s", file_path.name)
        return []

    # Vérifier la présence de données pixel
    if not hasattr(ds, "PixelData"):
        logger.info("Pas de données pixel dans %s", file_path.name)
        return []

    try:
        pixel_array = ds.pixel_array
    except Exception as e:
        logger.error("Impossible de décoder les pixels de %s: %s", file_path.name, e)
        return []

    # Conversion de l'espace colorimétrique
    # Pour les DICOM compressés (JPEG, JPEG2000, JPEG-LS), le décodeur
    # (pillow/pylibjpeg) convertit déjà en RGB. On ne convertit que
    # les données non compressées qui sont réellement en YCbCr.
    photometric = getattr(ds, "PhotometricInterpretation", "")
    is_compressed = _is_jpeg_transfer_syntax(ds)

    if photometric in ("YBR_FULL_422", "YBR_FULL", "YBR_ICT", "YBR_RCT"):
        if is_compressed:
            logger.debug("JPEG détecté, décodeur a déjà converti en RGB")
        else:
            pixel_array = cv2.cvtColor(pixel_array, cv2.COLOR_YCrCb2RGB)

    # Extraction des métadonnées pertinentes
    metadata = _extract_dicom_metadata(ds)
    # writer.py fixe PatientID = stem(metadata["source_file"]) : sans cette clé, PatientID vaudrait "UNKNOWN".
    metadata["source_file"] = file_path.name

    # Angiographie (FA / ICGA) : hors périmètre FREDD, aucune image produite.
    try:
        import analyse_contenu as _ac
        if any(c.get("modalite") in _ac.MODALITES_ANGIO for c in _ac.analyser_dicom(file_path)):
            logger.info("DICOM %s : angiographie exclue (hors périmètre FREDD)", file_path.name)
            return []
    except ImportError:
        pass

    # Déterminer le type d'image
    image_type = _classify_dicom_image(ds)
    if image_type == "autofluorescence" and not _af_par_texte(ds):
        metadata["af_presumee"] = True  # niveaux de gris sans mention d'AF : hypothèse FREDD, tracée
    if image_type == "fundus_grayscale":
        image_type = _classify_fundus_field(pixel_array)
    elif image_type in ("autofluorescence", "fundus_color"):
        try:
            premiere = pixel_array[0] if (int(getattr(ds, "NumberOfFrames", 1)) > 1 and pixel_array.ndim >= 3) else pixel_array
            if "champ" not in metadata:  # un montage reconstruit (métadonnées) garde son champ
                metadata["champ"] = champ_par_coins(premiere)
                metadata["champ_methode"] = "coins"
        except Exception as e:  # noqa: BLE001
            logger.warning("Champ non déterminé pour %s : %s", file_path.name, e)

    # Gestion multi-frame
    results = []
    num_frames = int(getattr(ds, "NumberOfFrames", 1))
    if num_frames > 1 and pixel_array.ndim >= 3:
        for i in range(num_frames):
            frame = pixel_array[i]
            results.append(ImageData(
                image=frame.copy(),
                metadata=metadata.copy(),
                source_file=str(file_path),
                image_index=i,
                image_type=image_type,
            ))
    else:
        results.append(ImageData(
            image=pixel_array.copy(),
            metadata=metadata,
            source_file=str(file_path),
            image_index=0,
            image_type=image_type,
        ))

    logger.info("DICOM %s: %d image(s) extraite(s), type=%s", file_path.name, len(results), image_type)
    return results


def _extract_dicom_metadata(ds: pydicom.Dataset) -> dict:
    """Extrait les métadonnées DICOM pertinentes."""

    def _get(attr: str, default=None):
        val = getattr(ds, attr, default)
        if val is not None and not isinstance(val, (str, int, float)):
            val = str(val)
        return val

    meta = {
        "patient_id": _get("PatientID", ""),
        "patient_name": _get("PatientName", ""),
        "study_date": _get("StudyDate", ""),
        "modality": _get("Modality", ""),
        "laterality": _get("ImageLaterality", _get("Laterality", "")),
        "manufacturer": _get("Manufacturer", ""),
        "model": _get("ManufacturerModelName", ""),
        "software_version": ("\\".join(str(x) for x in ds.SoftwareVersions)
                             if "SoftwareVersions" in ds and not isinstance(ds.SoftwareVersions, str)
                             else _get("SoftwareVersions", "")),
        "sop_class_uid": _get("SOPClassUID", ""),
        "sop_instance_uid": _get("SOPInstanceUID", ""),
        "study_instance_uid": _get("StudyInstanceUID", ""),
        "series_instance_uid": _get("SeriesInstanceUID", ""),
        "rows": _get("Rows", 0),
        "columns": _get("Columns", 0),
        "bits_allocated": _get("BitsAllocated", 8),
        "photometric": _get("PhotometricInterpretation", ""),
    }

    # Métadonnées ophtalmiques spécifiques
    # Date/heure d'acquisition et taille de pixel (mm),
    # pour qu'elles puissent être reportées dans le DICOM produit.
    meta["acquisition_datetime"] = _date_heure_dicom(ds)
    # Un export anonymisé écrase souvent les dates (constaté : 1900-01-01, 1973/1974). Une date hors
    # plage n'est pas recopiée : mieux vaut une date vide qu'une fausse date d'examen.
    if meta["acquisition_datetime"] and not date_plausible(meta["acquisition_datetime"]):
        meta["acquisition_datetime"] = ""
        meta["date_non_fiable"] = True
    if meta.get("study_date") and not date_plausible(meta["study_date"]):
        meta["study_date"] = ""
        meta["date_non_fiable"] = True
    meta["pixel_spacing"] = _espacement_dicom(ds)
    meta["camera_angle"] = _get_float(ds, "CameraAngleOfView")
    meta["horizontal_fov"] = _get_float(ds, "HorizontalFieldOfView")
    meta["image_type_tags"] = _get("ImageType", "")
    # Montage Zeiss CLARUS (ImageType MONTAGE, ≥ 2 sources codées 121329) : champ « reconstruit ».
    # Stocké comme métadonnée `champ`, pas comme nouveau type d'image (comme pour l'AF).
    tokens = {str(t).strip().upper() for t in (getattr(ds, "ImageType", None) or [])}
    n_src = sum(1 for it in (getattr(ds, "SourceImageSequence", None) or [])
                if any(str(getattr(c, "CodeValue", "")) == "121329"
                       for c in (getattr(it, "PurposeOfReferenceCodeSequence", None) or [])))
    if "MONTAGE" in tokens or n_src >= 2:
        meta["champ"] = "ultra_grand_champ_reconstruit"
        meta["champ_methode"] = "metadonnees_montage"
        meta["uwf"] = "montage"
    elif "UWF" in tokens:
        meta["uwf"] = "natif"  # capture simple étiquetée UWF par l'appareil (Clarus)

    return meta


ANNEE_MIN_PLAUSIBLE = 1990


def date_plausible(v) -> bool:
    """Vrai si la date DICOM (AAAAMMJJ[...]) tombe entre 1990 et l'an prochain. Faux sinon (ou illisible)."""
    try:
        annee = int(str(v).strip()[:4])
    except ValueError:
        return False
    from datetime import datetime as _dt
    return ANNEE_MIN_PLAUSIBLE <= annee <= _dt.now().year + 1


def _date_heure_dicom(ds: pydicom.Dataset) -> str:
    """AcquisitionDateTime, sinon AcquisitionDate+Time, sinon ContentDate+Time ("" si rien)."""
    v = str(getattr(ds, "AcquisitionDateTime", "") or "").strip()
    if v:
        return v
    for d, t in (("AcquisitionDate", "AcquisitionTime"), ("ContentDate", "ContentTime")):
        dv = str(getattr(ds, d, "") or "").strip()
        if dv:
            return dv + str(getattr(ds, t, "") or "").strip()
    return ""


def _espacement_dicom(ds: pydicom.Dataset):
    """[ligne, colonne] en mm (PixelSpacing, sinon ImagerPixelSpacing), ou None."""
    for kw in ("PixelSpacing", "ImagerPixelSpacing"):
        v = getattr(ds, kw, None)
        try:
            if v is not None and len(v) == 2 and float(v[0]) > 0 and float(v[1]) > 0:
                return [float(v[0]), float(v[1])]
        except (TypeError, ValueError):
            pass
    return None


def _is_jpeg_transfer_syntax(ds: pydicom.Dataset) -> bool:
    """Vérifie si le DICOM utilise une compression JPEG/JPEG2000/JPEG-LS."""
    try:
        ts = str(ds.file_meta.TransferSyntaxUID)
    except (AttributeError, KeyError):
        return False
    # Tous les transfer syntaxes JPEG commencent par 1.2.840.10008.1.2.4.
    return ts.startswith("1.2.840.10008.1.2.4.")


def _get_float(ds: pydicom.Dataset, attr: str) -> float | None:
    """Extrait un attribut DICOM en float, retourne None si absent."""
    val = getattr(ds, attr, None)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def champ_par_coins(image: np.ndarray) -> str:
    """« grand_champ » ou « maculaire » d'après les 4 coins de l'image (règle du
    module) : coins noirs = image circulaire sur fond noir = grand champ (Heidelberg 55°) ; sinon
    l'image remplit le cadre = champ maculaire (Heidelberg 30°). Même mesure que _classify_fundus_field.
    Pour l'AF et le fond d'œil couleur, ce champ est porté par les métadonnées (« champ »), pas par
    le type d'image, afin de ne rien changer aux types déjà utilisés en aval."""
    return "grand_champ" if _classify_fundus_field(image) == "fundus_widefield" else "maculaire"


def _classify_fundus_field(image: np.ndarray) -> str:
    """Distingue les fundus grand champ (circulaire) des maculaires (plein cadre).

    Grand champ : image circulaire sur fond noir → coins sombres, centre clair,
    et contour rétinien circulaire et net.
    Maculaire : l'image remplit tout le cadre → coins parfois assombris par
    vignettage, mais le contour de la zone éclairée n'est pas circulaire.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    h, w = gray.shape
    corner_size = min(h, w) // 8

    corners = [
        gray[:corner_size, :corner_size],
        gray[:corner_size, w - corner_size:],
        gray[h - corner_size:, :corner_size],
        gray[h - corner_size:, w - corner_size:],
    ]
    # Image trop petite pour y découper des coins : on ne peut rien conclure et on
    # retient le cas le plus fréquent. Sans ce garde-fou, corner_size valait 0, corner_mean
    # était NaN et la comparaison ci-dessous était fausse en silence (RuntimeWarning
    # « Mean of empty slice », visible dans test_perimetre_dicom).
    if corner_size == 0:
        return "fundus_macular"

    corner_mean = float(np.mean([c.mean() for c in corners]))

    # Fraction de pixels quasi-noirs (<10) dans les coins.
    # Un vrai widefield (cercle sur fond noir) a des coins massivement noirs,
    # tandis qu'un maculaire avec du vignettage a des coins sombres mais pas noirs.
    total_corner_px = sum(c.size for c in corners)
    black_corner_px = sum(int(np.count_nonzero(c < 10)) for c in corners)
    black_frac = black_corner_px / total_corner_px

    # Un critère « centre lumineux » (center_mean > 50) figurait ici en commentaire.
    # Décision de ne pas le réactiver : il ferait dépendre la détection du grand champ de la
    # luminosité rétinienne, alors que dans les dystrophies rétiniennes évoluées le centre peut
    # être atrophique et sombre — le grand champ serait alors reclassé maculaire à tort. Les
    # quatre coins noirs suffisent et ne dépendent que du cadrage, pas de l'état de la rétine.
    if corner_mean < 30 and black_frac > 0.5:
        return "fundus_widefield"
    return "fundus_macular"


def _af_par_texte(ds: pydicom.Dataset) -> bool:
    t = str(getattr(ds, "ImageType", "")).upper()
    return "AF" in t or "FAF" in t or "AUTOFLUOR" in t


def _classify_dicom_image(ds: pydicom.Dataset) -> str:
    """Détermine le type d'image à partir des tags DICOM."""
    image_type_str = str(getattr(ds, "ImageType", "")).upper()
    modality = str(getattr(ds, "Modality", "")).upper()
    samples = int(getattr(ds, "SamplesPerPixel", 1))

    # Autofluorescence
    if _af_par_texte(ds):
        return "autofluorescence"

    # Photo ophtalmique couleur
    if modality == "OP" and samples == 3:
        if "COLOR" in image_type_str:
            return "fundus_color"
        return "fundus_color"

    # Photo ophtalmique en niveaux de gris : un fond d'œil FREDD est en couleur, donc c'est
    # une autofluorescence PRÉSUMÉE (décision FREDD ; marquée « af_presumee » dans read_dicom).
    if modality == "OP" and samples == 1:
        return "autofluorescence"

    # OCT
    if modality == "OPT" or modality == "OCT":
        return "oct_bscan"

    # Fallback selon le nombre de canaux
    if samples == 3:
        return "fundus_color"
    return "fundus_grayscale"


# ---------------------------------------------------------------------------
# E2E (Heidelberg)
# ---------------------------------------------------------------------------

def _ouvrir_e2e(file_path: Path):
    """Ouvre un E2E avec une lecture des métadonnées TOLÉRANTE. oct_converter 0.7.0 lit
    `structure_data.text[0]` dans read_all_metadata() ; sur certains fichiers (observés : 12 du passage
    d'octobre 2026, Montpellier et REFERET) le bloc 9005 « structure examinée » a une liste de textes vide,
    d'où « list index out of range » à CHAQUE lecture, OCT comme fond d'œil, alors que les images sont
    intactes. Ces métadonnées (dump brut) ne servent pas au pipeline, qui lit le contenu via
    analyse_contenu : en cas d'échec on les remplace par un squelette vide plutôt que de perdre le fichier."""
    from oct_converter.readers import E2E

    class _E2ETolerant(E2E):
        def read_all_metadata(self):
            try:
                return super().read_all_metadata()
            except Exception as e:  # noqa: BLE001
                logger.warning("E2E %s : métadonnées oct_converter illisibles (%s: %s), squelette vide utilisé",
                               Path(self.filepath).name, type(e).__name__, e)
                meta = {k: [] for k in ("image_data", "bscan_data", "patient_data", "laterality_data",
                                        "contour_data", "fundus_data", "device_data", "localizer",
                                        "eye_data", "uid_data", "time_data", "additional_device_data")}
                meta.update(examined_structure={}, scan_pattern={}, enface_modality={}, oct_modality={})
                return meta

    return _E2ETolerant(str(file_path))


def read_e2e(file_path: Path) -> list[ImageData]:
    """Lit un fichier E2E et extrait les images de fond d'œil et les B-scans OCT."""
    try:
        from oct_converter.readers import E2E
    except ImportError:
        logger.error("oct_converter non installé, impossible de lire les fichiers E2E")
        return []

    results = []
    e2e = _ouvrir_e2e(file_path)

    # Exclusion des images infrarouges (décision FREDD :
    # les IR ne sont pas conservées). Lu une seule fois, réutilisé plus bas.
    try:
        oct_volumes = e2e.read_oct_volume()
    except Exception as e:
        logger.error("Erreur lecture OCT E2E %s: %s", file_path.name, e)
        oct_volumes = []
    series_ir, series_af, series_oct_analyse, infos_series = _series_ir_af(file_path)
    # Séries OCT : celles lues par oct_converter ET celles vues par l'analyse de contenu,
    # pour que l'IR de repérage reste exclue même si le B-scan n'a pas pu être décodé.
    series_oct = {_serie_e2e(getattr(v, "volume_id", "")) for v in oct_volumes} | series_oct_analyse
    # Angiographies (FA / ICGA) : hors périmètre FREDD, jamais exportées comme « fond d'œil ».
    series_angio = {s for s, l in infos_series.items() if l.get("modalite") in ("angiographie_fluo", "icga")}

    # --- Images de fond d'œil ---
    try:
        fundus_images = e2e.read_fundus_image()
        for i, fundus in enumerate(fundus_images):
            serie = _serie_e2e(getattr(fundus, "image_id", ""))
            if serie in series_angio:
                logger.info("E2E %s : angiographie exclue (série %s)", file_path.name, serie)
                continue
            if serie in series_ir or (serie in series_oct and serie not in series_af):
                logger.info("E2E %s : image IR exclue (série %s, %s)", file_path.name, serie,
                            "repérage d'un OCT" if serie in series_oct else "série infrarouge")
                continue
            img = _fundus_to_array(fundus)
            if img is None:
                continue

            meta = _extract_e2e_metadata(fundus, file_path)
            meta["e2e_image_type"] = "fundus"
            _completer_meta_e2e(meta, serie, infos_series, e2e)

            img_type = "fundus_grayscale"# if img.ndim == 2 else "fundus_color"
            if serie in series_af:
                # Sans cela, une AF d'E2E sortait en
                # fundus_macular/widefield : mauvaise étiquette, et la détection de
                # papille propre à l'AF (detection.py, image inversée) n'était jamais utilisée.
                img_type = "autofluorescence"
                # Heidelberg : 30° (maculaire) ou 55° (grand champ), distingués
                # par les 4 coins de l'image (règle du module).
                meta["champ"] = champ_par_coins(img)
                meta["champ_methode"] = "coins"
            elif img_type == "fundus_grayscale":
                img_type = _classify_fundus_field(img)

            results.append(ImageData(
                image=img,
                metadata=meta,
                source_file=str(file_path),
                image_index=i,
                image_type=img_type,
            ))
    except Exception as e:
        logger.error("Erreur lecture fundus E2E %s: %s", file_path.name, e)

    # --- Volumes OCT (B-scans individuels) ---
    try:
        bscan_index = len(results)
        for vol_i, volume in enumerate(oct_volumes):
            vol_array = _volume_to_array(volume)
            if vol_array is None:
                continue

            meta_base = _extract_e2e_metadata(volume, file_path)
            meta_base["e2e_image_type"] = "oct"
            meta_base["volume_id"] = str(getattr(volume, "volume_id", "") or vol_i)
            _completer_meta_e2e(meta_base, _serie_e2e(getattr(volume, "volume_id", "")), infos_series, e2e)

            # Extraire chaque B-scan individuellement
            if vol_array.ndim == 3:
                for b in range(vol_array.shape[0]):
                    bscan = vol_array[b]
                    meta = meta_base.copy()
                    meta["bscan_number"] = b
                    meta["total_bscans"] = vol_array.shape[0]

                    results.append(ImageData(
                        image=bscan,
                        metadata=meta,
                        source_file=str(file_path),
                        image_index=bscan_index,
                        image_type="oct_bscan",
                    ))
                    bscan_index += 1
            elif vol_array.ndim == 2:
                meta_base["bscan_number"] = 0
                meta_base["total_bscans"] = 1
                results.append(ImageData(
                    image=vol_array,
                    metadata=meta_base,
                    source_file=str(file_path),
                    image_index=bscan_index,
                    image_type="oct_bscan",
                ))
                bscan_index += 1

    except Exception as e:
        logger.error("Erreur lecture OCT E2E %s: %s", file_path.name, e)

    logger.info("E2E %s: %d image(s) extraite(s)", file_path.name, len(results))
    return results


def _completer_meta_e2e(meta: dict, serie: str, infos: dict, e2e) -> None:
    """Métadonnées d'intérêt d'une image E2E : appareil,
    date/heure d'acquisition (lue par oct_converter dans les blocs B-scan ; absente si
    le fichier n'a pas d'OCT), orientation de la coupe (analyse de contenu FREDD)."""
    info = infos.get(serie, {}) or {}
    # oct_converter met une taille de pixel latérale FIXE par défaut (0,01) et l'ordre de ses
    # axes ne correspond pas à PixelSpacing : on n'écrit rien plutôt qu'une valeur fausse.
    meta["pixel_spacing"] = None
    if not meta.get("laterality"):
        meta["laterality"] = {"OD": "R", "OG": "L"}.get(info.get("lateralite", ""), "")
    meta["manufacturer"] = "Heidelberg Engineering"
    meta["model"] = info.get("appareil", "") or ""
    meta["orientation"] = info.get("orientation", "") or ""
    meta["serie_e2e"] = serie
    acq = getattr(e2e, "acquisition_date", None)
    if acq is not None and hasattr(acq, "strftime"):
        meta["acquisition_datetime"] = acq.strftime("%Y%m%d%H%M%S")
        meta["study_date"] = acq.strftime("%Y%m%d")


def _serie_e2e(image_id) -> str:
    """'<patient_db>_<étude>_<série>' (identifiant oct_converter, avec éventuels
    '_' de répétition) -> '<étude>_<série>' (clé de analyse_contenu)."""
    parties = str(image_id or "").rstrip("_").split("_")
    return "_".join(parties[1:3]) if len(parties) >= 3 else ""


def _series_ir_af(file_path: Path) -> tuple[set, set, set, dict]:
    """Séries IR seules, et séries dont l'image en face est une
    autofluorescence (à garder même si elle sert de repérage à un OCT), d'après
    l'analyse de contenu du pipeline FREDD (analyse_contenu.py, à côté de ce package).
    Si elle n'est pas disponible, seules les IR de repérage des OCT sont exclues."""
    try:
        import analyse_contenu as ac
    except ImportError:
        logger.warning("analyse_contenu indisponible : séries IR seules non détectées")
        return set(), set(), set(), {}
    ir, af, oct_, infos = set(), set(), set(), {}
    for l in ac.analyser_e2e(file_path):
        if l.get("erreur"):
            continue
        infos[l.get("serie", "")] = l
        if l.get("modalite") == "infrarouge":
            ir.add(l.get("serie", ""))
        if l.get("modalite") == "oct_bscan":
            oct_.add(l.get("serie", ""))
        enface = next((x.split("=", 1)[1] for x in (l.get("detail") or "").split("; ")
                       if x.startswith("en_face=")), "")
        if l.get("modalite") == "autofluorescence" or ac.AF_MOTIF.search(enface):
            af.add(l.get("serie", ""))
    return ir, af, oct_, infos


def _fundus_to_array(fundus) -> np.ndarray | None:
    """Convertit une image fundus E2E en array numpy uint8."""
    try:
        if hasattr(fundus, "image"):
            img = fundus.image
        elif hasattr(fundus, "pixel_array"):
            img = fundus.pixel_array
        else:
            return None

        # PIL Image → numpy
        if hasattr(img, "convert"):
            img = np.array(img)

        if img is None or img.size == 0:
            return None

        # Normaliser en uint8
        if img.dtype == np.float64 or img.dtype == np.float32:
            img = (np.clip(img, 0, 1) * 255).astype(np.uint8)
        elif img.dtype != np.uint8:
            img = img.astype(np.uint8)

        return img
    except Exception as e:
        logger.warning("Impossible de convertir fundus E2E: %s", e)
        return None


def _est_image_en_face(img) -> bool:
    """Image 2D carrée : jamais un B-scan Heidelberg (496 x N) ; image en face (IR/SLO)
    décodée à tort comme de l'OCT par oct_converter."""
    return img.ndim == 2 and img.shape[0] == img.shape[1]


def _volume_to_array(volume) -> np.ndarray | None:
    """Convertit un volume OCT E2E en array numpy uint8."""
    try:
        if hasattr(volume, "volume"):
            vol = volume.volume
        elif hasattr(volume, "pixel_array"):
            vol = volume.pixel_array
        else:
            return None

        # Liste de B-scans → array 3D
        if isinstance(vol, list):
            # Un B-scan Heidelberg n'est JAMAIS carré (496 x N). Les blocs « image » carrés que
            # oct_converter range avec les coupes (ind 1) sont des images EN FACE à 8 bits (infrarouge de
            # repérage) : il les lit à tort comme de l'OCT à 16 bits, d'où des images illisibles (copies
            # dupliquées, mosaïque) et, quand l'une d'elles se glisse dans un volume, l'échec de
            # np.stack (« all input arrays must have the same shape ») qui perdait tout le volume.
            tableaux = [np.array(b) for b in vol]
            gardes = [b for b in tableaux if not _est_image_en_face(b)]
            if len(gardes) < len(tableaux):
                logger.info("E2E volume %s : %d image(s) carrée(s) en face (IR 8 bits) écartée(s) de l'OCT",
                            getattr(volume, "volume_id", "?"), len(tableaux) - len(gardes))
            if not gardes:
                return None
            vol = np.stack(gardes)
        elif hasattr(vol, "__array__"):
            vol = np.array(vol)
            if vol.ndim == 2 and _est_image_en_face(vol):  # voir ci-dessus
                return None

        if vol is None or vol.size == 0:
            return None

        # Normaliser en uint8
        if vol.dtype in (np.float64, np.float32):
            vol = (np.clip(vol, 0, 1) * 255).astype(np.uint8)
        elif vol.dtype != np.uint8:
            vmin, vmax = vol.min(), vol.max()
            if vmax > vmin:
                vol = ((vol - vmin) / (vmax - vmin) * 255).astype(np.uint8)
            else:
                vol = np.zeros_like(vol, dtype=np.uint8)

        return vol
    except Exception as e:
        logger.warning("Impossible de convertir volume OCT E2E: %s", e)
        return None


def _extract_e2e_metadata(obj, file_path: Path) -> dict:
    """Extrait les métadonnées d'un objet E2E (fundus ou volume)."""
    meta: dict = {
        "source_format": "E2E",
        "source_file": file_path.name,
        "patient_id": "",
        "laterality": "",
        "study_date": "",
        "modality": "OP",
    }

    # Patient ID
    if hasattr(obj, "patient_id"):
        meta["patient_id"] = str(obj.patient_id) if obj.patient_id else ""

    # Latéralité
    lat = _get_e2e_laterality(obj)
    if lat:
        meta["laterality"] = lat

    # Métadonnées supplémentaires du volume OCT
    if hasattr(obj, "pixel_spacing"):
        meta["pixel_spacing"] = obj.pixel_spacing

    # Qualité image OCT
    if hasattr(obj, "metadata") and isinstance(obj.metadata, dict):
        for key in ("imgQuality", "scanType", "scaley"):
            if key in obj.metadata:
                meta[key] = obj.metadata[key]

    return meta


def _get_e2e_laterality(obj) -> str:
    """Extrait la latéralité d'un objet E2E."""
    # Attribut direct
    if hasattr(obj, "laterality"):
        lat = obj.laterality
        if lat is not None:
            lat_str = str(lat).upper().strip()
            if lat_str in ("R", "OD", "RIGHT"):
                return "R"
            if lat_str in ("L", "OS", "LEFT"):
                return "L"
            # Codes ASCII: R=82, L=76
            if lat_str == "82":
                return "R"
            if lat_str == "76":
                return "L"

    # Chercher dans les métadonnées
    if hasattr(obj, "metadata") and isinstance(obj.metadata, dict):
        for key in ("laterality", "eye", "side"):
            val = obj.metadata.get(key)
            if val is not None:
                val_str = str(val).upper().strip()
                if val_str in ("R", "OD", "RIGHT", "82"):
                    return "R"
                if val_str in ("L", "OS", "LEFT", "76"):
                    return "L"

    return ""
