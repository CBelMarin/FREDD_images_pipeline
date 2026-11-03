"""Configuration du pipeline de prétraitement."""

from dataclasses import dataclass, field


@dataclass
class PipelineConfig:
    # Dimensions cibles
    fundus_target_size: tuple[int, int] = (2048, 2048)
    oct_target_size: tuple[int, int] = (496, 1024)

    # Seuils de qualité
    min_quality_score: float = 0.35
    min_sharpness: float = 0.15
    min_contrast: float = 0.10
    max_artifact_coverage: float = 0.50

    # Détection d'artefacts
    border_fraction: float = 0.12
    dark_threshold: int = 25
    edge_density_threshold: float = 0.04

    # Détection de cils (images grand champ / OPTOS)
    eyelash_min_coverage: float = 0.03  # couverture min pour signaler (3%)

    # Prétraitement
    clahe_clip_limit: float = 2.0
    clahe_grid_size: tuple[int, int] = (8, 8)
    denoise_strength_color: int = 7
    denoise_strength_gray: int = 10
    crop_intensity_threshold: int = 10

    # Détection du disque optique
    disc_blur_factor: float = 30.0
    disc_roi_factor: float = 6.0
    disc_morph_kernel_size: int = 15

    # Détection de la fovéa
    fovea_disc_distance_factor: float = 2.5
    fovea_search_radius_factor: float = 3.0

    # Types d'images reconnus
    image_types: dict = field(default_factory=lambda: {
        "fundus_color": "Fond d'œil couleur",
        "fundus_grayscale": "Fond d'œil niveaux de gris",
        "fundus_macular": "Fond d'œil maculaire (plein cadre)",
        "fundus_widefield": "Fond d'œil grand champ (circulaire)",
        "oct_bscan": "OCT B-scan",
        "autofluorescence": "Autofluorescence",
    })
