"""Génération du rapport récapitulatif du pipeline."""

import csv
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ImageReport:
    """Résultat du traitement d'une image individuelle."""
    source_file: str = ""
    image_index: int = 0
    image_type: str = ""
    original_shape: str = ""
    processed_shape: str = ""
    laterality: str = ""
    angle_of_view: str = ""
    optic_disc: str = ""
    fovea: str = ""
    quality_score: float = 0.0
    quality_issues: str = ""
    borders_cropped: bool = False
    accepted: bool = True
    rejection_reason: str = ""
    output_file: str = ""


@dataclass
class PipelineReport:
    """Rapport global du pipeline."""
    entries: list[ImageReport] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def accepted_count(self) -> int:
        return sum(1 for e in self.entries if e.accepted)

    @property
    def flagged_count(self) -> int:
        return sum(1 for e in self.entries if not e.accepted)

    @property
    def flagged_entries(self) -> list[ImageReport]:
        return [e for e in self.entries if not e.accepted]

    def add(self, entry: ImageReport) -> None:
        self.entries.append(entry)

    def write_csv(self, output_path: str | Path) -> Path:
        """Écrit le rapport au format CSV."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = [
            "source_file", "image_index", "image_type",
            "original_shape", "processed_shape",
            "laterality", "angle_of_view",
            "optic_disc", "fovea",
            "quality_score", "quality_issues",
            "borders_cropped",
            "accepted", "rejection_reason", "output_file",
        ]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for entry in self.entries:
                row = asdict(entry)
                row["quality_score"] = f"{entry.quality_score:.3f}"
                row["borders_cropped"] = "OUI" if entry.borders_cropped else "NON"
                row["accepted"] = "OUI" if entry.accepted else "À VÉRIFIER"
                writer.writerow(row)

        logger.info("Rapport CSV écrit: %s", output_path)
        return output_path

    def write_json(self, output_path: str | Path) -> Path:
        """Écrit le rapport au format JSON."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "summary": {
                "total_images": self.total,
                "accepted": self.accepted_count,
                "flagged": self.flagged_count,
            },
            "images": [asdict(e) for e in self.entries],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info("Rapport JSON écrit: %s", output_path)
        return output_path

    def write_flagged_summary(self, output_path: str | Path) -> Path:
        """Écrit un résumé des images signalées (qualité insuffisante)."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "=" * 70,
            "IMAGES À VÉRIFIER",
            "=" * 70,
            "",
            f"Total images traitées  : {self.total}",
            f"Images sans problème   : {self.accepted_count}",
            f"Images à vérifier      : {self.flagged_count}",
            "",
            "Les images ci-dessous ont été traitées mais présentent des",
            "problèmes de qualité potentiels (artefacts, cils, etc.).",
            "",
        ]

        if self.flagged_entries:
            lines.append("-" * 70)
            for entry in self.flagged_entries:
                lines.extend([
                    f"Fichier    : {entry.source_file}",
                    f"Index      : {entry.image_index}",
                    f"Type       : {entry.image_type}",
                    f"Score      : {entry.quality_score:.3f}",
                    f"Problème   : {entry.rejection_reason}",
                    f"Détails    : {entry.quality_issues}",
                    f"Sortie     : {entry.output_file}",
                    "-" * 70,
                ])
        else:
            lines.append("Aucune image à vérifier.")

        lines.append("")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info("Résumé écrit: %s", output_path)
        return output_path

    def print_summary(self) -> None:
        """Affiche un résumé dans la console."""
        print(f"\n{'=' * 50}")
        print(f"  RÉSUMÉ DU PIPELINE")
        print(f"{'=' * 50}")
        print(f"  Images traitées  : {self.total}")
        print(f"  Sans problème    : {self.accepted_count}")
        print(f"  À vérifier       : {self.flagged_count}")

        if self.flagged_entries:
            print(f"\n  Images à vérifier :")
            for e in self.flagged_entries:
                src = Path(e.source_file).name
                print(f"    - {src} [idx={e.image_index}] : {e.rejection_reason}")

        print(f"{'=' * 50}\n")
