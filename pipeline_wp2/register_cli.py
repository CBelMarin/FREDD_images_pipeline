"""CLI pour le recalage maculaire ↔ widefield.

Usage :
    # dossier
    python -m raretia.register_cli <dossier_dicom> -o <dossier_sortie>

    # archive zip (extraite récursivement dans un dossier temporaire)
    python -m raretia.register_cli input_recalage.zip -o <dossier_sortie>

    # fichier dicom unique (utile pour debug)
    python -m raretia.register_cli image.dcm -o <dossier_sortie>

Quand l'entrée est un zip, le dossier de sortie est nommé d'après le zip :
    -o output/  →  output/<nom_du_zip>/
"""

import argparse
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

from .registration import process_directory


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recalage des fundus maculaires sur les fundus widefield correspondants",
    )
    parser.add_argument(
        "input",
        help="Dossier, fichier DICOM, ou archive .zip contenant les DICOM fundus",
    )
    parser.add_argument(
        "-o", "--output",
        default="output_register",
        help="Dossier de sortie. Pour une entrée .zip, un sous-dossier "
             "portant le nom du zip y est créé. (défaut: ./output_register)",
    )
    parser.add_argument(
        "--expected-scale",
        type=float,
        default=0.5,
        help="Ratio de zoom attendu maculaire→widefield (≈ FOV_mac / FOV_wf, défaut 0.5)",
    )
    parser.add_argument(
        "--scale-tolerance",
        type=float,
        default=0.5,
        help="Tolérance relative autour de l'échelle attendue (défaut 0.5)",
    )
    parser.add_argument(
        "--min-inliers",
        type=int,
        default=15,
        help="Nombre minimum d'inliers RANSAC pour accepter une paire (défaut 15)",
    )
    parser.add_argument(
        "--no-overlay", action="store_true",
        help="Ne pas générer les images overlay",
    )
    parser.add_argument(
        "--no-warped", action="store_true",
        help="Ne pas générer les maculaires warpés",
    )
    parser.add_argument(
        "--no-side-by-side", action="store_true",
        help="Ne pas générer les vues côte-à-côte",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Logs détaillés",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Erreur: {input_path} n'existe pas", file=sys.stderr)
        sys.exit(1)

    output_base = Path(args.output)

    # --- Cas 1 : archive ZIP -----------------------------------------------
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        output_dir = output_base / input_path.stem
        with tempfile.TemporaryDirectory(prefix="raretia_register_") as tmp:
            tmp_path = Path(tmp)
            logging.info("Extraction de %s dans %s ...", input_path.name, tmp_path)
            _extract_zip_recursive(input_path, tmp_path)
            results = process_directory(
                tmp_path,
                output_dir,
                expected_scale=args.expected_scale,
                scale_tolerance=args.scale_tolerance,
                min_inliers=args.min_inliers,
                save_overlay=not args.no_overlay,
                save_warped=not args.no_warped,
                save_side_by_side=not args.no_side_by_side,
            )

    # --- Cas 2 : fichier DICOM unique --------------------------------------
    elif input_path.is_file():
        output_dir = output_base / input_path.stem
        # process_directory parcourt récursivement, donc on lui passe le parent
        # et on accepte qu'il puisse traiter d'autres fichiers du même dossier.
        results = process_directory(
            input_path.parent,
            output_dir,
            expected_scale=args.expected_scale,
            scale_tolerance=args.scale_tolerance,
            min_inliers=args.min_inliers,
            save_overlay=not args.no_overlay,
            save_warped=not args.no_warped,
            save_side_by_side=not args.no_side_by_side,
        )

    # --- Cas 3 : dossier ---------------------------------------------------
    elif input_path.is_dir():
        output_dir = output_base
        results = process_directory(
            input_path,
            output_dir,
            expected_scale=args.expected_scale,
            scale_tolerance=args.scale_tolerance,
            min_inliers=args.min_inliers,
            save_overlay=not args.no_overlay,
            save_warped=not args.no_warped,
            save_side_by_side=not args.no_side_by_side,
        )

    else:
        print(f"Erreur: {input_path} n'est ni un fichier ni un dossier exploitable", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("Aucune paire trouvée.", file=sys.stderr)
        sys.exit(1)

    accepted = sum(1 for r in results if r.accepted)
    print(f"\n{accepted}/{len(results)} paire(s) acceptée(s).")
    print(f"Rapport : {output_dir / 'pairs.csv'}")


def _extract_zip_recursive(zip_path: Path, dest: Path) -> None:
    """Extrait un zip et tous les zips imbriqués récursivement.

    Identique à raretia.main._extract_zip_recursive : un zip peut contenir
    d'autres zips (ex. un zip "lot" qui contient un zip par patient).
    """
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)

    for nested_zip in list(dest.rglob("*.zip")):
        nested_dest = nested_zip.with_suffix("")
        nested_dest.mkdir(parents=True, exist_ok=True)
        _extract_zip_recursive(nested_zip, nested_dest)
        nested_zip.unlink()


if __name__ == "__main__":
    main()
