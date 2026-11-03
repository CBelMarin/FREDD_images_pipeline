"""Prétraitement des images rétiniennes : recadrage, dimensions, colorimétrie, débruitage."""

import logging

import cv2
import numpy as np

from .config import PipelineConfig

logger = logging.getLogger(__name__)


def preprocess_image(
    image: np.ndarray,
    image_type: str,
    config: PipelineConfig | None = None,
) -> np.ndarray:
    """Applique la chaîne complète de prétraitement sur une image.

    Ordre : recadrage → débruitage → colorimétrie → dimensions.
    """
    if config is None:
        config = PipelineConfig()

    img = image.copy()

    # 1. Recadrage (suppression des bords noirs et blancs)
    if image_type == "oct_bscan":
        img = crop_oct_borders(img)
    else:
        img = crop_to_content(img, threshold=config.crop_intensity_threshold)

    # 2. Débruitage
    img = denoise(img, image_type, config)

    # 3. Standardisation colorimétrique (CLAHE + normalisation)
    img = standardize_colorimetry(img, image_type, config)

    # 4. Standardisation des dimensions
    target = config.oct_target_size if image_type == "oct_bscan" else config.fundus_target_size
    img = standardize_dimensions(img, target)

    # 5. Conversion en RGB (images niveaux de gris → 3 canaux)
    img = ensure_rgb(img)

    return img


# ---------------------------------------------------------------------------
# Conversion RGB
# ---------------------------------------------------------------------------

def ensure_rgb(image: np.ndarray) -> np.ndarray:
    """Convertit une image en RGB si elle est en niveaux de gris."""
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    if image.ndim == 3 and image.shape[2] == 1:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    return image


# ---------------------------------------------------------------------------
# Recadrage
# ---------------------------------------------------------------------------

def crop_to_content(image: np.ndarray, threshold: int = 10) -> np.ndarray:
    """Recadre l'image pour supprimer les bords noirs/vides.

    Pour les images de fond d'œil, la zone utile est généralement circulaire
    dans un cadre noir. On extrait le rectangle englobant.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image

    # Masque binaire des pixels non-noirs
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)

    # Nettoyage morphologique pour éviter les pixels isolés
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    # Trouver le contour englobant
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return image

    # Plus grand contour = zone rétinienne
    largest = max(contours, key=cv2.contourArea)

    # Vérifier que le contour couvre une surface significative
    contour_area = cv2.contourArea(largest)
    image_area = gray.shape[0] * gray.shape[1]
    if contour_area < image_area * 0.10:
        return image  # Pas de recadrage si la zone détectée est trop petite

    x, y, w, h = cv2.boundingRect(largest)

    # Marge de sécurité
    margin = max(2, min(w, h) // 200)
    x = max(0, x - margin)
    y = max(0, y - margin)
    w = min(image.shape[1] - x, w + 2 * margin)
    h = min(image.shape[0] - y, h + 2 * margin)

    cropped = image[y:y + h, x:x + w]

    logger.debug("Recadrage: (%d,%d) %dx%d → %dx%d", x, y, image.shape[1], image.shape[0], w, h)
    return cropped


def crop_oct_borders(image: np.ndarray, dark: int = 10, bright: int = 245,
                     ratio: float = 0.85) -> np.ndarray:
    """Recadre les bandes noires ET blanches autour d'un B-scan OCT.

    Une ligne (ou colonne) est considérée comme une bande si au moins *ratio*
    de ses pixels sont soit très sombres (< dark) soit très clairs (> bright).
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    h, w = gray.shape

    def is_border(line: np.ndarray) -> bool:
        n = line.size
        dark_frac = np.count_nonzero(line < dark) / n
        bright_frac = np.count_nonzero(line > bright) / n
        # Une ligne/colonne est un bord si elle est quasi-entièrement
        # composée de pixels très sombres et/ou très clairs (pas de contenu).
        return (dark_frac + bright_frac) >= ratio

    # Lignes (haut → bas, bas → haut)
    top = 0
    while top < h and is_border(gray[top]):
        top += 1
    bottom = h
    while bottom > top and is_border(gray[bottom - 1]):
        bottom -= 1

    # Colonnes (gauche → droite, droite → gauche)
    left = 0
    while left < w and is_border(gray[:, left]):
        left += 1
    right = w
    while right > left and is_border(gray[:, right - 1]):
        right -= 1

    # Sécurité : ne pas tout supprimer
    if (bottom - top) < h * 0.2 or (right - left) < w * 0.2:
        logger.debug("crop_oct_borders: recadrage trop agressif, image non modifiée")
        return image

    cropped = image[top:bottom, left:right]
    if cropped.shape != image.shape[:2] if image.ndim == 2 else cropped.shape != image.shape:
        logger.debug("crop_oct_borders: (%d,%d) %dx%d → %dx%d",
                     left, top, w, h, right - left, bottom - top)
    return cropped


# ---------------------------------------------------------------------------
# Débruitage
# ---------------------------------------------------------------------------

def denoise(image: np.ndarray, image_type: str, config: PipelineConfig) -> np.ndarray:
    """Applique un débruitage adapté au type d'image."""
    if image_type == "oct_bscan":
        return _denoise_oct(image, config)
    elif image_type in ("fundus_color",):
        return _denoise_color(image, config)
    else:
        return _denoise_grayscale(image, config)


def _denoise_color(image: np.ndarray, config: PipelineConfig) -> np.ndarray:
    """Débruitage pour images couleur (Non-Local Means)."""
    h = config.denoise_strength_color
    return cv2.fastNlMeansDenoisingColored(image, None, h, h, 7, 21)


def _denoise_grayscale(image: np.ndarray, config: PipelineConfig) -> np.ndarray:
    """Débruitage pour images en niveaux de gris."""
    h = config.denoise_strength_gray
    return cv2.fastNlMeansDenoising(image, None, h, 7, 21)


def _denoise_oct(image: np.ndarray, config: PipelineConfig) -> np.ndarray:
    """Débruitage pour B-scans OCT (filtre médian + bilatéral pour le speckle)."""
    # Filtre médian pour le bruit de speckle
    denoised = cv2.medianBlur(image, 3)
    # Filtre bilatéral pour lisser tout en préservant les couches rétiniennes
    denoised = cv2.bilateralFilter(denoised, 7, 50, 50)
    return denoised


# ---------------------------------------------------------------------------
# Standardisation colorimétrique
# ---------------------------------------------------------------------------

def standardize_colorimetry(
    image: np.ndarray,
    image_type: str,
    config: PipelineConfig,
) -> np.ndarray:
    """Normalise le contraste et la colorimétrie de l'image."""
    clip = config.clahe_clip_limit
    grid = config.clahe_grid_size
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=grid)

    if image.ndim == 3 and image.shape[2] == 3:
        # Image couleur : CLAHE sur le canal L en espace LAB
        lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        result = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    else:
        # Image en niveaux de gris : CLAHE direct
        result = clahe.apply(image)

    return result


# ---------------------------------------------------------------------------
# Standardisation des dimensions
# ---------------------------------------------------------------------------

def standardize_dimensions(
    image: np.ndarray,
    target_size: tuple[int, int],
) -> np.ndarray:
    """Redimensionne l'image aux dimensions cibles en préservant le ratio d'aspect.

    L'image est redimensionnée pour tenir dans target_size, puis centrée
    sur un fond noir de la taille exacte target_size.
    """
    target_h, target_w = target_size
    h, w = image.shape[:2]

    # Calcul du facteur d'échelle
    scale = min(target_w / w, target_h / h)

    new_w = int(w * scale)
    new_h = int(h * scale)

    # Choix de l'interpolation selon le sens du redimensionnement
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4
    resized = cv2.resize(image, (new_w, new_h), interpolation=interp)

    # Créer le canvas de sortie (noir)
    if image.ndim == 3:
        canvas = np.zeros((target_h, target_w, image.shape[2]), dtype=image.dtype)
    else:
        canvas = np.zeros((target_h, target_w), dtype=image.dtype)

    # Centrer l'image redimensionnée
    y_off = (target_h - new_h) // 2
    x_off = (target_w - new_w) // 2
    canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized

    return canvas
