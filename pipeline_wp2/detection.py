"""Détection du disque optique et de la fovéa dans les images de fond d'œil."""

import logging

import cv2
import numpy as np

from .config import PipelineConfig

logger = logging.getLogger(__name__)


def detect_optic_disc(
    image: np.ndarray,
    image_type: str,
    config: PipelineConfig | None = None,
) -> tuple[int, int, int] | None:
    """Détecte le disque optique dans une image de fond d'œil.

    Retourne (centre_x, centre_y, rayon) ou None si non détecté.

    Algorithme :
    1. Extraction du canal rouge (le plus contrasté pour le disque)
    2. Flou gaussien fort pour localiser la zone la plus brillante
    3. ROI autour du pic de luminosité
    4. Seuillage Otsu dans la ROI
    5. Analyse de contours et ajustement circulaire
    """
    if config is None:
        config = PipelineConfig()

    if image_type == "oct_bscan":
        return None  # Pas de détection de disque sur les B-scans OCT

    # Préparer les canaux candidats (rouge, vert, luminance)
    channels = _prepare_disc_channels(image, image_type)

    # Essayer chaque canal puis chaque percentile jusqu'à trouver le disque
    for gray in channels:
        for percentile in (85, 75, 65):
            result = _detect_disc_single_attempt(gray, image.shape[:2], percentile, config)
            if result is not None:
                return result

    logger.debug("Disque optique non détecté après toutes les tentatives")
    return None


def _prepare_disc_channels(image: np.ndarray, image_type: str) -> list[np.ndarray]:
    """Prépare les canaux candidats pour la détection du disque."""
    if image_type == "autofluorescence":
        if image.ndim == 3:
            return [255 - cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)]
        return [255 - image]

    if image.ndim == 3 and image.shape[2] >= 3:
        return [
            image[:, :, 0],                              # Rouge
            image[:, :, 1],                              # Vert
            cv2.cvtColor(image, cv2.COLOR_RGB2GRAY),     # Luminance
        ]
    return [image.copy()]


def _detect_disc_single_attempt(
    gray: np.ndarray,
    image_shape: tuple[int, int],
    percentile: int,
    config: PipelineConfig,
) -> tuple[int, int, int] | None:
    """Tente de détecter le disque sur un canal donné avec un seuil percentile."""
    h, w = image_shape

    # Masquer les bords noirs pour éviter les faux positifs
    mask = _create_retinal_mask(gray)
    gray_masked = cv2.bitwise_and(gray, gray, mask=mask)

    # --- Étape 1 : Localisation grossière (pic de luminosité) ---
    sigma = max(h, w) / config.disc_blur_factor
    blurred = cv2.GaussianBlur(gray_masked, (0, 0), sigmaX=sigma)
    _, _, _, max_loc = cv2.minMaxLoc(blurred, mask=mask)
    peak_x, peak_y = max_loc

    # --- Étape 2 : ROI resserrée autour du pic ---
    # ROI plus petite (1/10 de l'image) pour isoler le disque de la rétine brillante
    roi_half = max(h, w) // 10
    rx1 = max(0, peak_x - roi_half)
    ry1 = max(0, peak_y - roi_half)
    rx2 = min(w, peak_x + roi_half)
    ry2 = min(h, peak_y + roi_half)
    roi = gray[ry1:ry2, rx1:rx2]
    roi_mask = mask[ry1:ry2, rx1:rx2]

    if roi.size == 0:
        return None

    # --- Étape 3 : Seuillage par percentile (pas Otsu) ---
    # Otsu sépare la rétine du fond noir ; on veut séparer le disque de la rétine.
    # On utilise le 85e percentile des pixels rétiniens dans la ROI.
    roi_work = cv2.bitwise_and(roi, roi, mask=roi_mask)
    valid_pixels = roi_work[roi_mask > 0]
    if valid_pixels.size == 0:
        return None
    pct_threshold = int(np.percentile(valid_pixels, percentile))
    _, thresh = cv2.threshold(roi_work, pct_threshold, 255, cv2.THRESH_BINARY)
    thresh = cv2.bitwise_and(thresh, thresh, mask=roi_mask)

    # Nettoyage morphologique
    k_size = config.disc_morph_kernel_size
    k_size = max(5, int(k_size * max(h, w) / 2048))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
    # Érosion d'abord pour séparer le disque des zones brillantes adjacentes
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_ERODE, kernel, iterations=1)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    # --- Étape 4 : Trouver le meilleur contour circulaire ---
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    best = _select_best_disc_contour(contours, roi.shape)
    if best is None:
        return None

    (cx, cy), radius = cv2.minEnclosingCircle(best)

    # Coordonnées dans l'image complète
    disc_x = int(cx + rx1)
    disc_y = int(cy + ry1)
    disc_r = int(radius)

    # Validation : le disque ne doit pas être trop grand ni trop petit
    expected_min = max(h, w) * 0.01
    expected_max = max(h, w) * 0.15
    if disc_r < expected_min or disc_r > expected_max:
        logger.debug("Disque détecté de taille aberrante (r=%d, attendu [%.0f, %.0f])",
                      disc_r, expected_min, expected_max)
        return None

    logger.debug("Disque optique détecté: centre=(%d, %d), rayon=%d", disc_x, disc_y, disc_r)
    return (disc_x, disc_y, disc_r)


def detect_fovea(
    image: np.ndarray,
    disc_location: tuple[int, int, int] | None,
    laterality: str,
    image_type: str,
    config: PipelineConfig | None = None,
) -> tuple[int, int] | None:
    """Détecte la fovéa dans une image de fond d'œil.

    La fovéa est la zone la plus sombre de la macula, située environ
    2.5 diamètres de disque en temporal du disque optique.

    Retourne (centre_x, centre_y) ou None.
    """
    if config is None:
        config = PipelineConfig()

    if image_type == "oct_bscan":
        return None

    h, w = image.shape[:2]

    # Canal vert : meilleur contraste pour la fovéa
    if image.ndim == 3 and image.shape[2] >= 2:
        green = image[:, :, 1]
    else:
        green = image.copy()

    # Déterminer la zone de recherche
    if disc_location is not None:
        disc_x, disc_y, disc_r = disc_location
        offset = int(config.fovea_disc_distance_factor * disc_r * 2)
        search_r = int(config.fovea_search_radius_factor * disc_r)

        # Direction temporale selon la latéralité
        # Convention d'imagerie : pour l'œil droit, le temporal est à gauche du disque
        if laterality == "R":
            search_x = disc_x - offset
        elif laterality == "L":
            search_x = disc_x + offset
        else:
            # Latéralité inconnue : chercher du côté le plus éloigné du bord
            if disc_x < w // 2:
                search_x = disc_x + offset
            else:
                search_x = disc_x - offset

        search_y = disc_y
    else:
        # Sans disque optique : chercher près du centre de l'image
        search_x = w // 2
        search_y = h // 2
        search_r = min(h, w) // 6

    # Bornes de la zone de recherche
    sx1 = max(0, search_x - search_r)
    sy1 = max(0, search_y - search_r)
    sx2 = min(w, search_x + search_r)
    sy2 = min(h, search_y + search_r)

    if sx2 <= sx1 or sy2 <= sy1:
        return None

    roi = green[sy1:sy2, sx1:sx2]
    if roi.size == 0:
        return None

    # Masquer les pixels noirs (hors de la zone rétinienne)
    roi_mask = roi > 10

    if not roi_mask.any():
        return None

    # Flou pour stabiliser la détection
    blur_sigma = max(roi.shape) / 8
    blurred = cv2.GaussianBlur(roi.astype(np.float64), (0, 0), sigmaX=blur_sigma)

    # Masquer les zones hors rétine avec une valeur haute
    blurred[~roi_mask] = 999

    # Point le plus sombre = centre de la fovéa
    min_loc = np.unravel_index(np.argmin(blurred), blurred.shape)

    fovea_x = int(min_loc[1] + sx1)
    fovea_y = int(min_loc[0] + sy1)

    logger.debug("Fovéa détectée: (%d, %d)", fovea_x, fovea_y)
    return (fovea_x, fovea_y)


# ---------------------------------------------------------------------------
# Latéralité
# ---------------------------------------------------------------------------

def compute_laterality(
    image: np.ndarray,
    metadata: dict,
    disc_location: tuple[int, int, int] | None,
) -> str:
    """Détermine la latéralité (œil droit 'R' ou gauche 'L').

    Priorité :
    1. Métadonnées DICOM/E2E
    2. Position du disque optique (si détecté)
    3. 'U' (inconnu)
    """
    # 1. Métadonnées
    lat = metadata.get("laterality", "")
    if lat in ("R", "L"):
        return lat

    # 2. Position du disque optique
    if disc_location is not None:
        disc_x = disc_location[0]
        w = image.shape[1]
        # Convention : si le disque est dans la moitié droite → œil droit
        # (le disque est nasal, et le côté nasal de l'œil droit est à droite)
        if disc_x > w * 0.55:
            return "R"
        elif disc_x < w * 0.45:
            return "L"

    return "U"


# ---------------------------------------------------------------------------
# Angle de vue
# ---------------------------------------------------------------------------

def compute_angle_of_view(metadata: dict, image_type: str) -> float | None:
    """Extrait ou estime l'angle de vue (champ de vision) en degrés.

    Priorité :
    1. Métadonnées DICOM (CameraAngleOfView, HorizontalFieldOfView)
    2. Estimation par défaut selon le type d'appareil/image
    """
    # Depuis les métadonnées DICOM
    for key in ("camera_angle", "horizontal_fov"):
        val = metadata.get(key)
        if val is not None and val > 0:
            return float(val)
    
    # Estimations par défaut
    if image_type == "oct_bscan":
        return 30.0  # B-scans OCT = 30°

    # Le modèle d'appareil prime sur l'étiquette « grand champ » : un Optos
    # (200°) ou un Clarus (90°) reconnu comme grand champ par les coins n'est pas un 55°.
    model = str(metadata.get("model", "")).upper()
    if "OPTOS" in model:
        return 200.0
    if "CLARUS" in model:
        return 90.0

    # AF Heidelberg : 30° (maculaire) ou 55° (grand champ), d'après les coins (métadonnée « champ »)
    if image_type == "autofluorescence":
        return {"grand_champ": 55.0, "maculaire": 30.0}.get(metadata.get("champ"))
    
    if image_type == "fundus_widefield":
        return 55.0  # Champs larges = 55°
    
    if image_type == "fundus_macular":
        return 30.0  # Champs maculaires classiques = 30°
    
    # Valeurs par défaut
    model = metadata.get("model", "").upper()
    if "CLARUS" in model:
        return 90.0  # Zeiss CLARUS = 90°
    if "OPTOS" in model:
        return 200.0  # Optos = 200°

    return None


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _create_retinal_mask(gray: np.ndarray) -> np.ndarray:
    """Crée un masque binaire de la zone rétinienne (excluant les bords noirs)."""
    _, mask = cv2.threshold(gray, 15, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=2)
    return mask


def _select_best_disc_contour(
    contours: list,
    roi_shape: tuple[int, int],
) -> np.ndarray | None:
    """Sélectionne le contour le plus probable pour le disque optique.

    Critères : taille suffisante et forme approximativement circulaire.
    """
    roi_area = roi_shape[0] * roi_shape[1]
    min_area = roi_area * 0.005
    max_area = roi_area * 0.70

    candidates = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area or area > max_area:
            continue

        # Circularité : 4π·area / périmètre²
        perimeter = cv2.arcLength(c, True)
        if perimeter == 0:
            continue
        circularity = 4 * np.pi * area / (perimeter * perimeter)

        # Le disque optique est grossièrement circulaire (circularité > 0.4)
        if circularity > 0.3:
            candidates.append((c, area, circularity))

    if not candidates:
        # Fallback : prendre le plus grand contour
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) >= min_area:
            return largest
        return None

    # Sélectionner par taille × circularité
    candidates.sort(key=lambda x: x[1] * x[2], reverse=True)
    return candidates[0][0]
