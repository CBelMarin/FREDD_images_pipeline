"""Création de fichiers DICOM résultat avec image traitée et métadonnées sélectionnées."""

import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian
from pydicom.sequence import Sequence

logger = logging.getLogger(__name__)

# SOP Classes
SECONDARY_CAPTURE_SOP = "1.2.840.10008.5.1.4.1.1.7"
OPHTHALMIC_PHOTO_8BIT_SOP = "1.2.840.10008.5.1.4.1.1.77.1.5.1"


def write_processed_dicom(
    processed_image: np.ndarray,
    metadata: dict,
    laterality: str,
    angle_of_view: float | None,
    optic_disc: tuple[int, int, int] | None,
    fovea: tuple[int, int] | None,
    output_path: str | Path,
    image_type: str = "fundus_color",
) -> Path:
    """Crée un fichier DICOM contenant l'image traitée et les métadonnées clés.

    Métadonnées conservées : ID patient, latéralité, angle de vue.
    Métadonnées ajoutées : résultats de détection (disque, fovéa).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Créer le dataset DICOM
    file_meta = _create_file_meta(processed_image)
    ds = FileDataset(str(output_path), {}, file_meta=file_meta, preamble=b"\x00" * 128)

    # Transfer syntax
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    # --- Patient Module ---
    # Le Patient ID est dérivé du nom du fichier source (sans extension)
    source_name = Path(metadata.get("source_file", "UNKNOWN")).stem
    ds.PatientID = source_name
    ds.PatientName = ""

    # --- Study Module ---
    # UID d'étude REMAPPÉ (jamais l'UID d'origine, qui
    # permettrait de retrouver l'examen dans le PACS du centre) mais déterministe :
    # toutes les images d'un même examen (ou d'un même E2E) partagent le même UID.
    uid_source = str(metadata.get("study_instance_uid") or "").strip()
    ds.StudyInstanceUID = generate_uid(entropy_srcs=["FREDD-etude", uid_source or source_name])
    acq = str(metadata.get("acquisition_datetime") or "").strip()
    # Date d'examen d'origine (DICOM) ou date d'acquisition (E2E) ; vide si inconnue
    # (plus de date de traitement mise par défaut, qui passait pour une date d'examen).
    ds.StudyDate = str(metadata.get("study_date") or "").strip() or acq[:8]
    if acq:
        ds.AcquisitionDateTime = acq
    ds.StudyDescription = "Retinal Image Preprocessing Pipeline"

    # --- Series Module ---
    # B-scans d'un même volume OCT : même série, numérotés dans l'ordre du volume.
    volume_id = str(metadata.get("volume_id") or "")
    ds.SeriesInstanceUID = (generate_uid(entropy_srcs=["FREDD-serie", source_name, volume_id])
                            if image_type == "oct_bscan" and volume_id else generate_uid())
    ds.Modality = "OP" if image_type != "oct_bscan" else "OPT"
    ds.SeriesDescription = f"Processed {image_type}"

    # --- Instance Module ---
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = SECONDARY_CAPTURE_SOP
    num = metadata.get("bscan_number")
    ds.InstanceNumber = int(num) + 1 if isinstance(num, int) else 1
    if image_type == "oct_bscan":
        commentaire = []
        if isinstance(num, int) and metadata.get("total_bscans"):
            commentaire.append(f"B-scan {int(num) + 1}/{metadata['total_bscans']}")
        if metadata.get("orientation"):
            commentaire.append(f"orientation : {metadata['orientation']}")
        if commentaire:
            ds.ImageComments = " ; ".join(commentaire)
    else:
        # champ (grand champ / maculaire) lu par le pipeline FREDD dans ImageComments
        # « uwf : natif | montage » : mode UWF Zeiss, pour le catalogue seulement (la confrontation
        # SKEZIA ne distingue pas UWF et non-UWF).
        commentaire = []
        # ImageComments reste en ASCII pur : sans SpecificCharacterSet déclaré, le
        # répertoire DICOM par défaut est ISO-IR 6 et un accent n'est pas conforme — d'où
        # « methode » et « presumee » sans accent. La règle est vérifiée par
        # test_metadonnees_sorties ; elle ne passait jusqu'ici que parce qu'une division par zéro
        # dans _classify_fundus_field empêchait ce chemin de s'exécuter sur les petites images.
        if metadata.get("champ"):
            commentaire.append(f"champ : {metadata['champ']} (methode : {metadata.get('champ_methode', 'inconnue')})")
        if metadata.get("uwf"):
            commentaire.append(f"uwf : {metadata['uwf']}")
        if metadata.get("af_presumee"):
            commentaire.append("af : presumee (niveaux de gris, sans mention d'autofluorescence)")
        if commentaire:
            ds.ImageComments = " ; ".join(commentaire)
    if metadata.get("date_non_fiable"):
        note = "date d'acquisition d'origine non fiable (ignorée)"
        ds.ImageComments = f"{ds.ImageComments} ; {note}" if getattr(ds, "ImageComments", "") else note
    espacement = metadata.get("pixel_spacing_out")
    if espacement:
        ds.PixelSpacing = [f"{float(espacement[0]):.10g}", f"{float(espacement[1]):.10g}"]  # VR DS : 16 car. max

    # --- Equipment Module ---
    if metadata.get("manufacturer"):
        ds.Manufacturer = metadata["manufacturer"]
    if metadata.get("model"):
        ds.ManufacturerModelName = metadata["model"]
    if metadata.get("software_version"):
        # VR LO : 64 caractères au plus par valeur, valeurs séparées par « \ ».
        morceaux = [m.strip()[:64] for m in str(metadata["software_version"]).split("\\") if m.strip()]
        if morceaux:
            ds.SoftwareVersions = morceaux if len(morceaux) > 1 else morceaux[0]

    # --- Image Module ---
    _set_pixel_data(ds, processed_image)

    # --- Ophthalmic Module ---
    ds.ImageLaterality = laterality if laterality in ("R", "L") else ""

    if angle_of_view is not None:
        ds.CameraAngleOfView = float(angle_of_view)

    # --- Résultats de détection (tags privés via SeriesDescription étendue) ---
    # On stocke les coordonnées dans des éléments standard quand possible
    _add_detection_results(ds, optic_disc, fovea)

    # --- Informations de traitement ---
    image_type_tags = ["DERIVED", "SECONDARY", "PREPROCESSED"]
    if image_type == "fundus_widefield":
        image_type_tags.append("WIDEFIELD")
    elif image_type == "fundus_macular":
        image_type_tags.append("MACULAR")
    ds.ImageType = image_type_tags

    # Aucune date de contenu n'est écrite, et aucune description de dérivation :
    # du code mort les proposait en commentaire depuis l'origine, il est supprimé pour que la
    # décision soit lisible. Les dates des en-têtes sources sont jugées non fiables (voir la
    # règle de plausibilité côté pipeline FREDD), et DerivationDescription aurait recopié le
    # nom du fichier source, qui peut porter un identifiant.
    # À noter si la question revient : ContentDate/ContentTime désignent la date de PRODUCTION
    # de l'image dérivée, pas celle de l'acquisition ; les écrire serait donc exact et sans
    # risque d'identification. C'est un choix à rouvrir avec l'équipe, pas une impossibilité.

    # Sauvegarder
    ds.save_as(str(output_path))
    logger.info("DICOM écrit: %s", output_path.name)
    return output_path


# ---------------------------------------------------------------------------
# Construction du dataset
# ---------------------------------------------------------------------------

def _create_file_meta(image: np.ndarray) -> pydicom.Dataset:
    """Crée le File Meta Information Header."""
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = SECONDARY_CAPTURE_SOP
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    file_meta.ImplementationVersionName = "RARETIA_0.1"
    return file_meta


def _set_pixel_data(ds: FileDataset, image: np.ndarray) -> None:
    """Configure les attributs de données pixel dans le dataset DICOM."""
    # S'assurer que l'image est en uint8
    if image.dtype != np.uint8:
        if image.dtype in (np.float32, np.float64):
            image = (np.clip(image, 0, 1) * 255).astype(np.uint8)
        else:
            image = image.astype(np.uint8)

    h, w = image.shape[:2]
    ds.Rows = h
    ds.Columns = w
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0  # unsigned

    if image.ndim == 3 and image.shape[2] == 3:
        ds.SamplesPerPixel = 3
        ds.PhotometricInterpretation = "RGB"
        ds.PlanarConfiguration = 0  # interleaved
        ds.PixelData = image.tobytes()
    else:
        # Niveaux de gris
        if image.ndim == 3:
            image = image[:, :, 0]
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = image.tobytes()

    ds.NumberOfFrames = 1


def _add_detection_results(
    ds: FileDataset,
    optic_disc: tuple[int, int, int] | None,
    fovea: tuple[int, int] | None,
) -> None:
    """Ajoute les résultats de détection anatomique au dataset DICOM.

    Utilise des Annotation Sequences quand possible, sinon des attributs texte.
    """
    annotations = []

    if optic_disc is not None:
        disc_x, disc_y, disc_r = optic_disc
        ann = Dataset()
        ann.ConceptNameCodeSequence = _make_code_sequence(
            "T-AA200", "SRT", "Optic Disc"
        )
        ann.GraphicType = "CIRCLE"
        ann.NumberOfGraphicPoints = 2
        # Centre et point sur le cercle
        ann.GraphicData = [
            float(disc_x), float(disc_y),
            float(disc_x + disc_r), float(disc_y),
        ]
        annotations.append(ann)

    if fovea is not None:
        fovea_x, fovea_y = fovea
        ann = Dataset()
        ann.ConceptNameCodeSequence = _make_code_sequence(
            "T-AA610", "SRT", "Fovea"
        )
        ann.GraphicType = "POINT"
        ann.NumberOfGraphicPoints = 1
        ann.GraphicData = [float(fovea_x), float(fovea_y)]
        annotations.append(ann)

    if annotations:
        ds.GraphicAnnotationSequence = Sequence(annotations)


def _make_code_sequence(value: str, scheme: str, meaning: str) -> Sequence:
    """Crée une séquence de code DICOM."""
    code = Dataset()
    code.CodeValue = value
    code.CodingSchemeDesignator = scheme
    code.CodeMeaning = meaning
    return Sequence([code])
