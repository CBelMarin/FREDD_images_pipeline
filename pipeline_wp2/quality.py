"""Évaluation de la qualité des images et détection d'artefacts (cils, flou, etc.)."""

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import PipelineConfig

logger = logging.getLogger(__name__)


@dataclass
class QualityReport:
    """Résultat de l'évaluation qualité d'une image."""
    score: float = 1.0
    accepted: bool = True
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def rejection_reason(self) -> str:
        if self.accepted:
            return ""
        return " ; ".join(self.issues)

    @property
    def all_remarks(self) -> list[str]:
        return self.issues + self.warnings


def assess_quality(
    image: np.ndarray,
    image_type: str,
    config: PipelineConfig | None = None,
    metadata: dict | None = None,
) -> QualityReport:
    """Évalue la qualité globale d'une image rétinienne.

    Les métriques (netteté, contraste, illumination) sont informatives et
    reportées comme avertissements. Seules les obstructions sévères (cils,
    doigt) couvrant la zone utile de l'image provoquent un rejet.
    """
    if config is None:
        config = PipelineConfig()

    report = QualityReport()
    scores: list[float] = []

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image

    # --- Métriques informatives (avertissements, jamais de rejet) ---

    # 1. Netteté (variance du Laplacien)
    sharpness = _compute_sharpness(gray)
    report.metrics["sharpness"] = sharpness
    sharpness_score = min(1.0, sharpness / 500.0)
    scores.append(sharpness_score)
    if sharpness_score < config.min_sharpness:
        report.warnings.append(f"Image floue (netteté: {sharpness_score:.2f})")

    # 2. Contraste (écart-type normalisé)
    contrast = gray.std() / 128.0
    report.metrics["contrast"] = contrast
    contrast_score = min(1.0, contrast)
    scores.append(contrast_score)
    if contrast_score < config.min_contrast:
        report.warnings.append(f"Contraste insuffisant ({contrast_score:.2f})")

    # 3. Illumination (intensité moyenne)
    mean_intensity = float(gray.mean())
    report.metrics["mean_intensity"] = mean_intensity
    if mean_intensity < 20:
        report.warnings.append(f"Image trop sombre (intensité: {mean_intensity:.0f})")
        scores.append(0.1)
    elif mean_intensity > 240:
        report.warnings.append(f"Image surexposée (intensité: {mean_intensity:.0f})")
        scores.append(0.1)
    else:
        scores.append(0.8)

    # 4. Uniformité d'illumination (rapport entre quadrants)
    uniformity = _compute_uniformity(gray)
    report.metrics["uniformity"] = uniformity
    scores.append(min(1.0, uniformity))
    if uniformity < 0.3:
        report.warnings.append(f"Illumination non uniforme ({uniformity:.2f})")

    # --- Problèmes de qualité sévères (fundus macular et widefield uniquement) ---
    if image_type in ("fundus_macular", "fundus_widefield"):
        qi_metrics, qi_issues = _detect_quality_issues(gray, metadata)
        report.metrics.update(qi_metrics)
        report.issues.extend(qi_issues)

    # --- Détection d'artefacts ---

    # if image_type in ("fundus_color", "fundus_grayscale", "fundus_macular", "fundus_widefield", "autofluorescence"):
    #     artifact_score, artifact_issues = _detect_artifacts(gray, config)
    #     report.metrics["artifact_score"] = artifact_score
    #     scores.append(artifact_score)
    #     report.issues.extend(artifact_issues)

    # --- Détection de cils (fundus color OPTOS uniquement) ---
    if image_type == "fundus_color" and _is_optos(metadata) and _is_circular_field(gray):
        eyelash_score, eyelash_coverage, eyelash_issues = _detect_peripheral_obstructions(gray, config)
        report.metrics["eyelash_score"] = eyelash_score
        report.metrics["eyelash_coverage"] = eyelash_coverage
        if eyelash_issues:
            scores.append(eyelash_score)
            report.issues.extend(eyelash_issues)

    # Score global (informatif)
    report.score = sum(scores) / len(scores) if scores else 0.0
    report.metrics["overall_score"] = report.score

    # Décision : seules les obstructions sévères rejettent
    report.accepted = len(report.issues) == 0

    if not report.accepted:
        logger.info("Image rejetée (score=%.2f): %s", report.score, report.rejection_reason)

    return report


# ---------------------------------------------------------------------------
# Métriques individuelles
# ---------------------------------------------------------------------------

def _detect_quality_issues(
    gray: np.ndarray,
    metadata: dict | None = None,
    _max_dim: int = 512,
) -> tuple[dict, list[str]]:
    """Détecte les problèmes de qualité sévères sur la zone rétinienne.

    Problèmes détectés :
    - Image trop sombre (médiane rétinienne faible)
    - Image surexposée (pixels saturés)
    - Bruit excessif (écart entre image et version filtrée médiane)
    - Image tronquée (cercle englobant déborde du cadre)
    - Image floue (faible niveau de détails normalisé)

    Les images > 512 px sont réduites pour la performance.
    """
    h, w = gray.shape
    issues: list[str] = []
    metrics: dict = {}

    # --- Réduction pour la performance (grands DICOM) ---
    if max(h, w) > _max_dim:
        scale = _max_dim / max(h, w)
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        small = gray
    sh, sw = small.shape

    # --- Masque rétinien (exclure le fond noir) ---
    _, retinal_mask = cv2.threshold(small, 15, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    retinal_mask = cv2.morphologyEx(retinal_mask, cv2.MORPH_OPEN, k, iterations=2)
    rp = retinal_mask > 0
    retinal_pixels = int(rp.sum())

    if retinal_pixels < sh * sw * 0.05:
        issues.append("Zone rétinienne insuffisante")
        return metrics, issues

    retinal_values = small[rp]

    # --- Métriques ---
    retinal_mean = float(retinal_values.mean())
    p50 = float(np.percentile(retinal_values, 50))
    metrics["retinal_mean"] = retinal_mean
    metrics["retinal_median"] = p50

    saturation = float((retinal_values > 240).sum()) / retinal_pixels
    metrics["saturation_ratio"] = saturation

    med = cv2.medianBlur(small, 5)
    noise_std = float((small.astype(np.float32) - med.astype(np.float32))[rp].std())
    metrics["noise_level"] = noise_std

    retinal_ratio = retinal_pixels / (sh * sw)
    metrics["retinal_ratio"] = retinal_ratio

    # Niveau de détails : écart-type du Laplacien (débruité) / intensité moyenne
    lap = cv2.Laplacian(med, cv2.CV_64F)
    detail_level = float(lap[rp].std()) / (retinal_mean + 1)
    metrics["detail_level"] = detail_level

    # --- Détection ---

    # 1. Image trop sombre (médiane résiste aux hotspots comme le disque optique)
    is_dark = p50 < 65
    if is_dark:
        issues.append(f"Image trop sombre (intensité médiane: {p50:.0f})")

    # 2. Image surexposée
    if saturation > 0.15:
        issues.append(f"Image surexposée ({saturation:.0%} de pixels saturés)")

    # 3. Bruit excessif
    is_noisy = noise_std > 10
    if is_noisy:
        issues.append(f"Bruit excessif (niveau: {noise_std:.1f})")

    # 4. Image tronquée (cercle englobant déborde du cadre)
    #    Uniquement pour les images à champ circulaire (ratio < 0.90).
    if retinal_ratio < 0.90:
        contours, _ = cv2.findContours(
            retinal_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )
        if contours:
            largest = max(contours, key=cv2.contourArea)
            (cx, cy), radius = cv2.minEnclosingCircle(largest)
            if radius > 0:
                overflow = max(
                    max(0.0, -(cx - radius)),
                    max(0.0, (cx + radius) - sw),
                    max(0.0, -(cy - radius)),
                    max(0.0, (cy + radius) - sh),
                ) / radius
                metrics["crop_overflow"] = overflow
                if overflow > 0.35:
                    issues.append(f"Image tronquée (débordement: {overflow:.0%})")

    # 5. Image floue (seulement si ni sombre ni bruitée, car ces
    #    problèmes réduisent aussi le niveau de détails).
    #    Exclu pour OPTOS : le grand champ a naturellement moins de détails/px.
    # if not is_dark and not is_noisy and not _is_optos(metadata) and detail_level < 0.12:
    #     issues.append(f"Image floue (niveau de détails: {detail_level:.3f})")

    return metrics, issues


def _is_optos(metadata: dict | None) -> bool:
    """Vérifie si l'image provient d'un appareil OPTOS."""
    if metadata is None:
        return False
    manufacturer = str(metadata.get("manufacturer", "")).upper()
    model = str(metadata.get("model", "")).upper()
    return "OPTOS" in manufacturer or "OPTOS" in model


def _compute_sharpness(gray: np.ndarray) -> float:
    """Calcule la netteté via la variance du Laplacien."""
    h, w = gray.shape
    if max(h, w) > 1024:
        scale = 1024 / max(h, w)
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        small = gray

    laplacian = cv2.Laplacian(small, cv2.CV_64F)
    return float(laplacian.var())


def _compute_uniformity(gray: np.ndarray) -> float:
    """Mesure l'uniformité de l'illumination via le rapport min/max des quadrants."""
    h, w = gray.shape
    mid_h, mid_w = h // 2, w // 2

    quadrants = [
        gray[:mid_h, :mid_w],
        gray[:mid_h, mid_w:],
        gray[mid_h:, :mid_w],
        gray[mid_h:, mid_w:],
    ]

    means = [float(q.mean()) for q in quadrants]
    max_mean = max(means)
    min_mean = min(means)

    if max_mean == 0:
        return 0.0
    return min_mean / max_mean


def _detect_artifacts(gray: np.ndarray, config: PipelineConfig) -> tuple[float, list[str]]:
    """Détecte les artefacts sévères (cils, obstructions) aux bords de l'image.

    Un artefact est signalé (et provoque un rejet) uniquement si TOUTES ces
    conditions sont réunies :
    - Zone sombre étendue (>70% de la bande) dans les bords haut/bas
    - Forte densité de contours directionnels (structure allongée type cils)
    - Les bords noirs naturels des images circulaires sont exclus
    """
    h, w = gray.shape
    border = int(h * 0.08)  # 8% de la hauteur seulement
    issues: list[str] = []
    artifact_score = 1.0

    is_circular = _is_circular_field(gray)

    regions = {
        "haut": gray[:border, :],
        "bas": gray[h - border:, :],
    }

    for name, strip in regions.items():
        # Pour les images circulaires, ne compter que le tiers central
        if is_circular:
            margin = int(strip.shape[1] * 0.30)
            strip = strip[:, margin:-margin] if margin > 0 else strip

        # Ratio de pixels très sombres (seuil bas pour éviter les faux positifs)
        dark_ratio = float((strip < 15).sum()) / strip.size

        if dark_ratio < 0.50:
            continue  # Pas assez de zone sombre pour être un artefact

        # Vérifier la structure des bords : les cils créent des contours
        # allongés et orientés, contrairement au bruit ou aux bords circulaires
        edges = cv2.Canny(strip, 80, 200)
        edge_density = float(edges.sum()) / (edges.size * 255)

        if edge_density < 0.08:
            continue  # Pas assez de structure pour des cils

        # Vérifier que les contours sont allongés (morphologie directionnelle)
        # Un kernel horizontal détecte les structures type cils
        kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1))
        edges_h = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel_h)
        horizontal_ratio = float(edges_h.sum()) / (edges.sum() + 1)

        if horizontal_ratio < 1.5:
            continue  # Contours non directionnels → probablement pas des cils

        # Tous les critères remplis → artefact confirmé
        issues.append(
            f"Cils détectés en {name} (couverture: {dark_ratio:.0%}, "
            f"densité bords: {edge_density:.3f})"
        )
        artifact_score -= 0.4 * dark_ratio

    return max(0.0, artifact_score), issues


def _is_circular_field(gray: np.ndarray) -> bool:
    """Détecte si l'image a un champ circulaire (bords noirs naturels)."""
    h, w = gray.shape
    corner_size = min(h, w) // 8

    corners = [
        gray[:corner_size, :corner_size],
        gray[:corner_size, w - corner_size:],
        gray[h - corner_size:, :corner_size],
        gray[h - corner_size:, w - corner_size:],
    ]
    center = gray[h // 3:2 * h // 3, w // 3:2 * w // 3]

    corner_mean = np.mean([c.mean() for c in corners])
    center_mean = center.mean()

    return corner_mean < 30 and center_mean > 50


def _detect_peripheral_obstructions(
    gray: np.ndarray,
    config: PipelineConfig,
    _max_dim: int = 512,
) -> tuple[float, float, list[str]]:
    """Détecte les obstructions périphériques (cils, paupières) sur les images
    à champ circulaire (OPTOS, grand champ).

    Méthode : on compare le masque rétinien observé avec une version
    « comblée » par fermeture morphologique agressive. Les zones présentes
    dans le masque comblé mais absentes du masque observé correspondent
    aux intrusions sombres (cils, paupières) depuis la périphérie.

    Le traitement est effectué sur une version réduite de l'image (512 px)
    car les ratios de couverture sont invariants à l'échelle.

    Returns:
        (score, coverage, issues)
        - score : 0.0 (obstruction totale) à 1.0 (aucune obstruction)
        - coverage : fraction de la zone rétinienne obstruée
        - issues : messages descriptifs des problèmes détectés
    """
    h, w = gray.shape
    issues: list[str] = []

    # --- Réduction pour la performance ---
    if max(h, w) > _max_dim:
        scale = _max_dim / max(h, w)
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        small = gray
    sh, sw = small.shape

    # --- 1. Masque rétinien (pixels non-noirs) ---
    _, retinal_mask = cv2.threshold(small, 15, 255, cv2.THRESH_BINARY)
    k_clean = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    retinal_mask = cv2.morphologyEx(retinal_mask, cv2.MORPH_OPEN, k_clean, iterations=2)
    retinal_mask = cv2.morphologyEx(retinal_mask, cv2.MORPH_CLOSE, k_clean, iterations=1)

    retinal_pixels = int(retinal_mask.sum() / 255)
    if retinal_pixels < sh * sw * 0.10:
        return 1.0, 0.0, []

    # --- 2. Masque « comblé » par fermeture morphologique ---
    close_size = max(sh, sw) // 10
    close_size += 1 - close_size % 2  # forcer impair
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_size, close_size))
    filled = cv2.morphologyEx(retinal_mask, cv2.MORPH_CLOSE, k_close, iterations=3)

    # --- 3. Zones d'intrusion = comblé − observé ---
    intrusion = cv2.subtract(filled, retinal_mask)

    # Retirer le bruit (très petites zones, irrégularités naturelles du bord)
    min_blob = max(3, int(np.sqrt(retinal_pixels) * 0.015))
    min_blob += 1 - min_blob % 2  # forcer impair
    k_denoise = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (min_blob, min_blob))
    intrusion = cv2.morphologyEx(intrusion, cv2.MORPH_OPEN, k_denoise)

    intrusion_pixels = int(intrusion.sum() / 255)
    filled_pixels = int(filled.sum() / 255)
    coverage = intrusion_pixels / filled_pixels if filled_pixels > 0 else 0.0

    if coverage < config.eyelash_min_coverage:
        return 1.0, coverage, []

    # --- 4. Validation structurelle ---
    edges = cv2.Canny(small, 40, 120)
    edges_in_intrusion = cv2.bitwise_and(edges, intrusion)
    edge_density = float(edges_in_intrusion.sum()) / (intrusion_pixels * 255 + 1)

    if edge_density < 0.005 and coverage < 0.08:
        return 1.0, coverage, []

    # --- 5. Localisation des secteurs affectés ---
    sectors = _identify_affected_sectors(intrusion, sh, sw)
    loc = ", ".join(sectors) if sectors else "périphérie"

    issues.append(
        f"Cils/artefacts détectés en {loc} "
        f"(couverture: {coverage:.0%} de la zone rétinienne)"
    )

    score = max(0.0, 1.0 - coverage * 2.5)
    return score, coverage, issues


def _identify_affected_sectors(
    intrusion_mask: np.ndarray,
    h: int,
    w: int,
) -> list[str]:
    """Identifie les secteurs (haut, bas, gauche, droite) affectés par les intrusions."""
    mid_h, mid_w = h // 2, w // 2

    quadrants = {
        "haut": intrusion_mask[:mid_h, :],
        "bas": intrusion_mask[mid_h:, :],
        "gauche": intrusion_mask[:, :mid_w],
        "droite": intrusion_mask[:, mid_w:],
    }

    total = float(intrusion_mask.sum())
    if total == 0:
        return []

    sectors: list[str] = []
    for name, region in quadrants.items():
        if float(region.sum()) / total > 0.25:
            sectors.append(name)

    return sectors
