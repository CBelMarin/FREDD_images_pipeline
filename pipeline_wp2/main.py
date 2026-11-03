"""Point d'entrée CLI pour le pipeline de prétraitement d'images rétiniennes."""

import argparse
import logging
import sys
from pathlib import Path

from .config import PipelineConfig
from .pipeline import RetinalPipeline


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline de prétraitement d'images rétiniennes (DICOM / E2E)",
    )
    parser.add_argument(
        "input",
        help="Répertoire ou fichier d'entrée contenant les fichiers DICOM/E2E",
    )
    parser.add_argument(
        "-o", "--output",
        default="output",
        help="Répertoire de sortie (défaut: ./output)",
    )
    parser.add_argument(
        "--target-size",
        type=int,
        nargs=2,
        default=[2048, 2048],
        metavar=("H", "W"),
        help="Dimensions cibles pour les images de fond d'œil (défaut: 2048 2048)",
    )
    parser.add_argument(
        "--oct-target-size",
        type=int,
        nargs=2,
        default=[496, 1024],
        metavar=("H", "W"),
        help="Dimensions cibles pour les B-scans OCT (défaut: 496 1024)",
    )
    parser.add_argument(
        "--min-quality",
        type=float,
        default=0.35,
        help="Score qualité minimum pour accepter une image (défaut: 0.35)",
    )
    parser.add_argument(
        "--denoise-strength",
        type=int,
        default=7,
        help="Intensité du débruitage (défaut: 7)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Mode verbeux (affiche les détails de chaque étape)",
    )

    args = parser.parse_args()

    # Configuration du logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Configuration du pipeline
    config = PipelineConfig(
        fundus_target_size=tuple(args.target_size),
        oct_target_size=tuple(args.oct_target_size),
        min_quality_score=args.min_quality,
        denoise_strength_color=args.denoise_strength,
    )

    # Lancer le pipeline
    pipeline = RetinalPipeline(config)
    input_path = Path(args.input)

    # Dossier de sortie nommé d'après l'entrée (sans extension)
    output_base = Path(args.output)
    output_dir = output_base / input_path.stem

    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            _extract_zip_recursive(input_path, Path(tmp))
            report = pipeline.process_directory(tmp, output_dir)
    elif input_path.is_file():
        report = pipeline.process_file(input_path, output_dir)
    elif input_path.is_dir():
        report = pipeline.process_directory(input_path, output_dir)
    else:
        print(f"Erreur: {input_path} n'existe pas", file=sys.stderr)
        sys.exit(1)

    # Code de retour
    if report.total == 0:
        print("Aucune image trouvée.", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


def _extract_zip_recursive(zip_path: Path, dest: Path) -> None:
    """Extrait un zip et tous les zips imbriqués récursivement."""
    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)

    # Chercher et extraire les zips imbriqués
    for nested_zip in list(dest.rglob("*.zip")):
        nested_dest = nested_zip.with_suffix("")  # dossier du même nom sans .zip
        nested_dest.mkdir(parents=True, exist_ok=True)
        _extract_zip_recursive(nested_zip, nested_dest)
        nested_zip.unlink()  # supprimer le zip imbriqué après extraction


if __name__ == "__main__":
    main()
