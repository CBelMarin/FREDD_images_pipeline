# FREDD image pipeline

Offline pipeline that inventories, classifies, deduplicates, pseudonymizes and standardizes heterogeneous multicenter ophthalmic images (DICOM, Heidelberg E2E, nested archives), and reconciles the images found with the images declared in an electronic case report form (eCRF). It was developed for the French Rare Eye Disease Database (FREDD) and run inside a secure processing environment without Internet access.

Version 1.12.6. The code, comments and console messages are in French; the documentation is in English.

> **Scope.** This is research code written for one project. It follows FREDD conventions (patient pseudonym in the folder name, a CSV export of the patient data, a specific eCRF layout) that you will probably have to adapt. It is not a medical device, comes with no warranty, and does not make data anonymous: images are pseudonymized, and patients remain linked to the database through the pseudonym.

![Architecture of the pipeline](docs/figure1_pipeline.png)

A detailed version of the architecture is in [`docs/figure_appendix_detailed_pipeline.png`](docs/figure_appendix_detailed_pipeline.png).

## What it does

1. **Inventory.** Walks the folders, reads nested zip archives in memory, computes the SHA-256 of each file and identifies its format (DICOM, E2E, standard image, other).
2. **Content analysis.** Reads headers without decoding pixels: modality, eye, OCT section orientation, device, wide-field montages, angiographies. Files with no image (DICOMDIR, proprietary Raw Data) are recognized here.
3. **Sorting.** Reads the patient pseudonym from the folder or file name, matches it to the patient data, assigns the center, removes exact copies (same SHA-256 and same patient) and gives **one status and one filing location to each source file**. Files that cannot be attached to a known patient are set aside, never guessed.
4. **Pixel preprocessing.** Calls an external pixel-processing module (cropping, quality score, denoising and CLAHE, resizing, optic disc and fovea detection) which writes a **new** DICOM file with whitelisted metadata only. Work is done in packets of 200 images and can resume after an interruption (resumption key: SHA-256 and patient).
5. **Reports and reconciliation.** Per-file log, per-center summary, run report with a prioritized action list, image catalog, and a per-patient comparison of declared and found images (modality, eye, OCT orientation or autofluorescence field).

### Safeguards after step 4

Each DICOM produced is checked independently: empty `PatientName`, `PatientID` equal to the expected pseudonym, no direct-identification tag, no source identifier in any text field, study UID remapped, and a pixel text detector (no OCR) that looks for aligned letter-sized blobs. A file that fails goes to quarantine and is never transmitted. The detector has known limits: it was measured on synthetic positive controls and misses low-contrast text (see `controles_positifs.py`). A visual check of a stratified sample (`outils/controle_visuel.py`) bounds the residual rate but cannot exclude it.

### Output folders

| Folder | Content |
|---|---|
| `images_traitees/<center>/<date>/<id>/` | pseudonymized DICOM files, one dated batch per run |
| `quarantaine_pseudonymisation/` | outputs that failed a safeguard |
| `sans_patient/<center>/` | files with an unknown, undetected or ambiguous pseudonym |
| `non_traitees_format/<center>/` | unsupported formats, Raw Data, infrared-only files |
| `echecs_traitement/<center>/` | sources for which the processing module produced nothing |
| `rapports/` | `rapport_execution.md` (open this first), `journal_images.csv`, `sorties_traitees.csv`, `confrontation_patients.csv`, `execution.json` |

## Installation

Python 3.10 to 3.13 (3.12 is the validated version). pylibjpeg-libjpeg (GPL-3.0) is installed separately from PyPI and is not redistributed with this repository.

```
python -m pip install -r requirements.txt
```

`requirements-lock.txt` pins the full environment. For a machine without Internet access, the scripts in [`installation/`](installation/) prepare a portable Python or offline wheels on a connected machine, then install them (see [`installation/INSTALLATION_HORS_LIGNE.md`](installation/INSTALLATION_HORS_LIGNE.md)). On Linux, OpenCV needs the system library `libgl1`.

## Usage

```
python fredd.py
```

The first run creates `config_fredd.ini` from [`config_fredd.exemple.ini`](config_fredd.exemple.ini): set the image folder, the patient data CSV and an output folder (outside any synchronized folder, with tens of GB of space). Then, from the menu or directly:

| Command | Purpose |
|---|---|
| `python fredd.py verifier` | checks Python, dependencies, the processing module, paths and CSV columns |
| `python fredd.py simulation` | inventory, sorting and reconciliation without processing, written to `<output>_simulation` |
| `python fredd.py traiter` | full processing; rerun at each new delivery, only new images are processed |
| `python fredd.py visionneuse` | local viewer (127.0.0.1 only): shows images, detected content, and remaining identifying information (blurred by default) |
| `python fredd.py exporter` | splits the processed images into ZIP archives of bounded size with SHA-256 checksums |
| `python fredd.py echantillon`, `doublons`, `patients`, `catalogue`, `texte`, `fovea`, `tests` | sampling, duplicate analysis, patient and image catalogs, text-check, foveal B-scan marking, test suite |

`pipeline_fredd.py --help` lists every option.

## Conventions to adapt

- **Pseudonym.** The patient folder (and normally each file) must start with the pseudonym, either complete or truncated to 8 characters; it is compared to the identifier column of the CSV on its first 8 characters.
- **Center.** Taken from a column of the CSV (`--col-centre`) or from the first-level folder. Several labels for one center can be merged with the alias table [`alias_centres.csv`](alias_centres.csv), loaded automatically from the folder of `pipeline_fredd.py`. The provided file holds a single example rule: edit it.
- **eCRF columns.** The reconciliation reads declared images from an eCRF export; the preset `fredd` describes the FREDD layout. Other layouts require changing the column mapping (see `pipeline_fredd.py`).
- **Excluded by project decision.** Infrared images are not produced; angiographies are out of scope; wide-field montages are excluded when all their native captures are present.

## Pixel-processing module

`pipeline_wp2/` performs the pixel processing of step 4: reading of DICOM and E2E files, cropping, quality score, denoising and CLAHE, resizing, optic disc and fovea detection, and writing of the new DICOM file. It is called through [`raretia_adapter.py`](raretia_adapter.py), the only point of contact between the pipeline and the module (`--raretia-path` designates the folder that contains it). The pipeline stops at startup if the package cannot be imported.

The package was adapted for FREDD (version string `0.1.0+fredd.6`): the DICOM `PatientID` carries the pseudonym, infrared images are not produced, autofluorescence from E2E files is typed as such, and the E2E reading was corrected.

## Tests

```
python -m pytest tests
```

The suite (481 tests, 2 skipped) checks each step with synthetic files: no real image or patient is needed. `tests/fake_raretia/` is a stand-in for the pixel module used by the pipeline-level tests, and `tests/test_pipeline_wp2_reel.py` runs the real package end to end.

## Repository tools

[`outils/`](outils/) contains the scripts that compute every figure and table of the associated article from the reports (`chiffres_papier.py`), audit the headers of delivered files (`audit_sorties_dicom.py`), compare two runs (`comparer_passages.py`), draw the stratified sample for the visual check (`controle_visuel.py`), and draw the two architecture figures.

## Data protection

Never put images, patient data, reports or `config_fredd.ini` in a repository: file paths and reports can identify patients. The provided `.gitignore` excludes the usual formats and output folders. Outputs of the pipeline contain no patient name, but they must still be handled inside a secure environment.

<!-- TO ADD AT PUBLICATION: license section, citation of the article (DOI), citation of the pixel-processing module, acknowledgments. -->
