"""Orchestration du pipeline de prétraitement complet."""

import logging
from pathlib import Path

import cv2
import numpy as np

from .config import PipelineConfig
from .reader import read_file, ImageData
from .preprocessing import preprocess_image, crop_to_content, crop_oct_borders
from .quality import assess_quality, QualityReport
from .detection import detect_optic_disc, detect_fovea, compute_laterality, compute_angle_of_view
from .writer import write_processed_dicom
from .report import PipelineReport, ImageReport

logger = logging.getLogger(__name__)


class RetinalPipeline:
    """Pipeline de prétraitement d'images rétiniennes."""

    def __init__(self, config: PipelineConfig | None = None):
        self.config = config or PipelineConfig()
        self.report = PipelineReport()

    def process_directory(self, input_dir: str | Path, output_dir: str | Path) -> PipelineReport:
        """Traite tous les fichiers DICOM et E2E d'un répertoire."""
        input_dir = Path(input_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Trouver tous les fichiers exploitables dans toute l'arborescence
        files = sorted(
            f for f in input_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in (".dcm", ".dicom", ".e2e")
        )

        if not files:
            logger.warning("Aucun fichier DICOM ou E2E trouvé dans %s", input_dir)
            return self.report

        logger.info("Traitement de %d fichier(s) depuis %s", len(files), input_dir)

        i = 0
        n = len(files)
        for file_path in files:
            print(i, "/", n, " - ", file_path)
            # L'ID = nom du dossier de plus haut niveau sous input_dir
            patient_id = _get_patient_folder(file_path, input_dir)
            self._process_file(file_path, output_dir, patient_id)
            i = i + 1

        # Écrire les rapports
        self._write_reports(output_dir)

        return self.report

    def process_file(self, file_path: str | Path, output_dir: str | Path) -> PipelineReport:
        """Traite un seul fichier."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        self._process_file(Path(file_path), output_dir)
        self._write_reports(output_dir)

        return self.report

    def _process_file(self, file_path: Path, output_dir: Path, patient_id: str = "") -> None:
        """Traitement interne d'un fichier."""
        logger.info("Lecture de %s ...", file_path.name)

        images = read_file(file_path)
        if not images:
            logger.warning("Aucune image extraite de %s", file_path.name)
            return

        # Injecter l'ID patient dans toutes les images
        if patient_id:
            for img_data in images:
                img_data.metadata["patient_id"] = patient_id

        logger.info("%d image(s) extraite(s) de %s (id=%s)", len(images), file_path.name, patient_id)

        for img_data in images:
            self._process_single_image(img_data, output_dir)

    def _process_single_image(self, img_data: ImageData, output_dir: Path) -> None:
        """Traite une image individuelle à travers tout le pipeline."""
        source_name = Path(img_data.source_file).name
        idx = img_data.image_index
        img_type = img_data.image_type
        original_shape = img_data.image.shape

        logger.info("  [%s idx=%d] type=%s, shape=%s", source_name, idx, img_type, original_shape)

        # --- Étape 1 : Recadrage initial (pour l'évaluation qualité) ---
        if img_type == "oct_bscan":
            cropped = crop_oct_borders(img_data.image)
        else:
            cropped = crop_to_content(img_data.image, threshold=self.config.crop_intensity_threshold)

        # --- Étape 2 : Évaluation de la qualité ---
        quality: QualityReport = assess_quality(cropped, img_type, self.config, img_data.metadata)

        borders_cropped = cropped.shape != img_data.image.shape

        entry = ImageReport(
            source_file=source_name,
            image_index=idx,
            image_type=img_type,
            original_shape=str(original_shape),
            quality_score=quality.score,
            quality_issues=" | ".join(quality.all_remarks) if quality.all_remarks else "",
            borders_cropped=borders_cropped,
        )

        if not quality.accepted:
            entry.accepted = False
            entry.rejection_reason = quality.rejection_reason
            logger.info("    ⚠ Qualité insuffisante (score=%.2f): %s",
                         quality.score, quality.rejection_reason)

        # --- Étape 3 : Prétraitement complet ---
        processed = preprocess_image(img_data.image, img_type, self.config)
        entry.processed_shape = str(processed.shape)

        # --- Étape 4 : Détection du disque optique ---
        disc = None
        if img_type != "oct_bscan":
            disc = detect_optic_disc(processed, img_type, self.config)
            if disc:
                entry.optic_disc = f"({disc[0]}, {disc[1]}, r={disc[2]})"
                logger.info("    Disque optique: centre=(%d,%d), rayon=%d", *disc)

        # --- Étape 5 : Latéralité ---
        laterality = compute_laterality(processed, img_data.metadata, disc)
        entry.laterality = laterality

        # --- Étape 6 : Détection de la fovéa ---
        fovea = None
        if img_type != "oct_bscan":
            fovea = detect_fovea(processed, disc, laterality, img_type, self.config)
            if fovea:
                entry.fovea = f"({fovea[0]}, {fovea[1]})"
                logger.info("    Fovéa: (%d, %d)", *fovea)

        # --- Étape 7 : Angle de vue ---
        angle = compute_angle_of_view(img_data.metadata, img_type)
        # Seul un angle MESURÉ (métadonnées source) est
        # écrit dans le DICOM ; une valeur par défaut (30°, 55°, 90°…) n'y figure plus,
        # car elle y passerait pour une mesure. Le rapport l'indique « estimé ».
        angle_mesure = any((img_data.metadata.get(k) or 0) > 0 for k in ("camera_angle", "horizontal_fov"))
        entry.angle_of_view = (f"{angle:.1f}°" + ("" if angle_mesure else " (estimé)")) if angle else "N/A"

        # Taille de pixel recalculée après recadrage +
        # redimensionnement (preprocess_image applique le même recadrage que ci-dessus,
        # puis une mise à l'échelle uniforme centrée sur fond noir).
        img_data.metadata["pixel_spacing_out"] = _espacement_sortie(
            img_data.metadata.get("pixel_spacing"), cropped.shape[:2],
            self.config.oct_target_size if img_type == "oct_bscan" else self.config.fundus_target_size)

        # --- Étape 8 : Création du DICOM résultat ---
        output_filename = _make_output_filename(
            img_data.metadata.get("patient_id", "unknown"),
            laterality,
            img_type,
            idx,
        )
        # Sous-dossier par patient (= nom du dossier parent du fichier source)
        patient_id = img_data.metadata.get("patient_id", "unknown")
        output_path = output_dir / "processed" / patient_id / output_filename
        output_path = _ensure_unique_path(output_path)

        write_processed_dicom(
            processed_image=processed,
            metadata=img_data.metadata,
            laterality=laterality,
            angle_of_view=angle if angle_mesure else None,
            optic_disc=disc,
            fovea=fovea,
            output_path=output_path,
            image_type=img_type,
        )

        entry.output_file = str(output_path.relative_to(output_dir))
        self.report.add(entry)

        logger.info("    → OK: lat=%s, angle=%s, output=%s",
                     laterality, entry.angle_of_view, output_filename)

    def _write_reports(self, output_dir: Path) -> None:
        """Écrit tous les fichiers de rapport."""
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)

        self.report.write_csv(report_dir / "rapport_complet.csv")
        self.report.write_json(report_dir / "rapport_complet.json")
        self.report.write_flagged_summary(report_dir / "images_a_verifier.txt")
        self.report.print_summary()


def _get_patient_folder(file_path: Path, base_dir: Path) -> str:
    """Retourne le nom du dossier patient/examen contenant le fichier.

    Le dossier patient est le parent immédiat du fichier, sauf si celui-ci
    est un sous-dossier de latéralité (OD, OS, …), auquel cas on remonte
    d'un niveau supplémentaire.

    Exemples :
        base_dir  = /tmp/extracted/
        file_path = /tmp/extracted/root/mid_folder/folder_1/fichier.dcm
        → "folder_1"

        file_path = /tmp/extracted/root/mid_folder_1/mid_folder_2/folder_1/OD/fichier.dcm
        → "folder_1"
    """
    _LATERALITY_FOLDERS = {"od", "os"}

    try:
        relative = file_path.relative_to(base_dir)
    except ValueError:
        return file_path.stem

    parts = relative.parts
    if len(parts) <= 1:
        # Fichier directement dans base_dir
        return file_path.stem

    # Parent immédiat du fichier
    parent_name = parts[-2]
    # Si c'est un dossier de latéralité, remonter d'un niveau
    if parent_name.lower() in _LATERALITY_FOLDERS and len(parts) >= 3:
        return parts[-3]
    return parent_name


def _espacement_sortie(espacement, forme_recadree, cible):
    """Taille de pixel (mm, [ligne, colonne]) après mise à
    l'échelle de forme_recadree vers cible (même calcul que standardize_dimensions)."""
    if not espacement:
        return None
    h, w = forme_recadree
    th, tw = cible
    if not h or not w:
        return None
    scale = min(tw / w, th / h)
    new_h, new_w = int(h * scale), int(w * scale)
    if not new_h or not new_w:
        return None
    return [float(espacement[0]) * h / new_h, float(espacement[1]) * w / new_w]


def _ensure_unique_path(path: Path) -> Path:
    """Ajoute un suffixe numérique si le fichier existe déjà pour éviter l'écrasement."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    candidate = parent / f"{stem}_{counter}{suffix}"
    while candidate.exists():
        counter += 1
        candidate = parent / f"{stem}_{counter}{suffix}"
    return candidate


def _make_output_filename(
    patient_id: str,
    laterality: str,
    image_type: str,
    index: int,
) -> str:
    """Génère un nom de fichier DICOM de sortie descriptif."""
    # Nettoyer le patient ID
    pid = patient_id.replace("/", "_").replace("\\", "_").replace(" ", "_")
    if len(pid) > 20:
        pid = pid[:20]
    if not pid:
        pid = "unknown"

    lat = laterality if laterality in ("R", "L") else "U"

    type_abbrev = {
        "fundus_color": "fundus_color",
        "fundus_grayscale": "fundus_gray",
        "fundus_macular": "fundus_macular",
        "fundus_widefield": "fundus_widefield",
        "oct_bscan": "oct_bscan",
        "autofluorescence": "autofluor",
    }.get(image_type, image_type)

    return f"{pid}_{lat}_{type_abbrev}_{index:03d}.dcm"
