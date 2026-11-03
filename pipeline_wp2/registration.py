"""Recalage d'images entre fundus maculaires et fundus grand champ (widefield).

Le module fait deux choses :

1. **Appariement (matching)** : pour chaque image maculaire, déterminer à
   quelle image widefield elle correspond (au sein du même patient et du même
   œil). Le score d'appariement est le nombre d'inliers d'une homographie
   estimée par RANSAC sur des correspondances de descripteurs AKAZE.

2. **Recalage (registration)** : appliquer cette homographie pour projeter
   l'image maculaire dans le repère du widefield, et produire des
   visualisations (overlay, mosaïque côte à côte, masque de la zone
   maculaire dans le widefield).

Hypothèses :
- L'image maculaire (~30° de champ) est une portion zoomée du widefield
  (~55° de champ). Le ratio d'échelle linéaire attendu est donc ≈ 0.5.
- Les fichiers DICOM portent la latéralité dans le tag ImageLaterality
  ou Laterality, et/ou dans leur nom (`_L_` / `_R_` à la convention raretia).
- Les images sont de la même série patient (groupées par dossier).
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pydicom

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------

@dataclass
class FundusImage:
    """Représente une image fundus chargée depuis un DICOM."""
    path: Path
    image: np.ndarray  # uint8, RGB ou niveaux de gris
    laterality: str    # "R", "L", ou ""
    image_type: str    # "fundus_macular" ou "fundus_widefield"
    patient_id: str = ""
    fov_degrees: float | None = None


@dataclass
class RegistrationResult:
    """Résultat d'un recalage d'une image maculaire sur un widefield."""
    macular_path: Path
    widefield_path: Path
    homography: np.ndarray | None
    num_matches: int
    num_inliers: int
    inlier_ratio: float
    estimated_scale: float | None
    score: float                       # critère pour choisir la meilleure paire
    reprojection_error: float | None   # erreur médiane (en pixels) sur inliers
    accepted: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Lecture DICOM
# ---------------------------------------------------------------------------

_LAT_RE = re.compile(r"_([LR])_", re.IGNORECASE)


def _laterality_from_name(name: str) -> str:
    m = _LAT_RE.search(name)
    if m:
        return m.group(1).upper()
    return ""


_KNOWN_OTHER_TYPES = ("fundus_color", "oct_bscan", "autofluor", "fundus_gray")


def _type_from_name(name: str) -> str:
    n = name.lower()
    if "fundus_widefield" in n or "widefield" in n:
        return "fundus_widefield"
    if "fundus_macular" in n or "macular" in n:
        return "fundus_macular"
    return ""


def _is_other_known_type(name: str) -> bool:
    n = name.lower()
    return any(tok in n for tok in _KNOWN_OTHER_TYPES)


def load_fundus_dicom(path: Path) -> FundusImage | None:
    """Charge un DICOM fundus et retourne une FundusImage, ou None si inutilisable."""
    try:
        ds = pydicom.dcmread(str(path))
    except Exception as e:
        logger.warning("Impossible de lire %s: %s", path.name, e)
        return None

    if not hasattr(ds, "PixelData"):
        return None

    try:
        arr = ds.pixel_array
    except Exception as e:
        logger.warning("Impossible de décoder %s: %s", path.name, e)
        return None

    if arr.dtype != np.uint8:
        arr = cv2.normalize(arr, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    photometric = str(getattr(ds, "PhotometricInterpretation", ""))
    if photometric.startswith("YBR") and arr.ndim == 3:
        try:
            arr = cv2.cvtColor(arr, cv2.COLOR_YCrCb2RGB)
        except cv2.error:
            pass

    lat = str(getattr(ds, "ImageLaterality", "") or getattr(ds, "Laterality", "")).upper().strip()
    if lat not in ("L", "R"):
        lat = _laterality_from_name(path.name)

    img_type = _type_from_name(path.name)
    if not img_type:
        # Si le nom indique explicitement un autre type connu (fundus_color,
        # OCT, autofluo, …), on n'essaie pas de le forcer en macular/widefield.
        if _is_other_known_type(path.name):
            return None
        # Tentative à partir des tags : un widefield a souvent un FOV plus large
        fov = getattr(ds, "CameraAngleOfView", None) or getattr(ds, "HorizontalFieldOfView", None)
        try:
            fov_f = float(fov) if fov is not None else None
        except (TypeError, ValueError):
            fov_f = None
        if fov_f is not None and 45 <= fov_f <= 80:
            img_type = "fundus_widefield"
        elif fov_f is not None and 15 <= fov_f < 45:
            img_type = "fundus_macular"
        else:
            return None

    fov_val = getattr(ds, "CameraAngleOfView", None) or getattr(ds, "HorizontalFieldOfView", None)
    try:
        fov_f = float(fov_val) if fov_val is not None else None
    except (TypeError, ValueError):
        fov_f = None

    return FundusImage(
        path=path,
        image=arr,
        laterality=lat,
        image_type=img_type,
        patient_id=str(getattr(ds, "PatientID", "") or ""),
        fov_degrees=fov_f,
    )


# ---------------------------------------------------------------------------
# Préparation des images pour la détection de features
# ---------------------------------------------------------------------------

def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        img = img[..., :3]
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)


def _content_mask(gray: np.ndarray, threshold: int = 10) -> np.ndarray:
    """Masque binaire (uint8 0/255) du contenu rétinien (hors bordures noires)."""
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    # Supprime les petits points hors rétine
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return mask


def _enhance_for_features(img: np.ndarray) -> np.ndarray:
    """Améliore le contraste pour la détection de features (CLAHE sur le canal vert)."""
    if img.ndim == 3:
        g = img[..., 1]  # canal vert : meilleure information vasculaire
    else:
        g = img
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(g)


def _make_detector(kind: str = "akaze"):
    """Retourne (detector, norm). 'sift' = meilleure invariance d'échelle, 'akaze' = rapide."""
    if kind == "sift":
        try:
            return cv2.SIFT_create(nfeatures=4000), cv2.NORM_L2
        except (AttributeError, cv2.error):
            pass
    return cv2.AKAZE_create(), cv2.NORM_HAMMING


# ---------------------------------------------------------------------------
# Recalage d'une paire (macular, widefield)
# ---------------------------------------------------------------------------

def _attempt_register(
    macular_img: np.ndarray,
    widefield_img: np.ndarray,
    pre_scale: float,
    detector_kind: str,
    ratio_test: float,
) -> tuple[np.ndarray | None, int, int, float | None, float | None]:
    """Une tentative de recalage avec une pré-échelle donnée du maculaire.

    Retourne (H_corrigée, num_matches, num_inliers, estimated_scale, reproj_err).
    H_corrigée projette le maculaire **original** (non redimensionné) dans
    le repère du widefield.
    """
    if abs(pre_scale - 1.0) > 1e-3:
        new_w = max(8, int(macular_img.shape[1] * pre_scale))
        new_h = max(8, int(macular_img.shape[0] * pre_scale))
        mac_resized = cv2.resize(macular_img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    else:
        mac_resized = macular_img

    g_mac = _to_gray(mac_resized)
    g_wf = _to_gray(widefield_img)
    mac_feat = _enhance_for_features(mac_resized)
    wf_feat = _enhance_for_features(widefield_img)
    mask_mac = _content_mask(g_mac)
    mask_wf = _content_mask(g_wf)

    detector, norm = _make_detector(detector_kind)
    kp_mac, des_mac = detector.detectAndCompute(mac_feat, mask_mac)
    kp_wf, des_wf = detector.detectAndCompute(wf_feat, mask_wf)
    if des_mac is None or des_wf is None or len(kp_mac) < 4 or len(kp_wf) < 4:
        return None, 0, 0, None, None

    matcher = cv2.BFMatcher(norm, crossCheck=False)
    knn = matcher.knnMatch(des_mac, des_wf, k=2)
    good = []
    for pair in knn:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < ratio_test * n.distance:
            good.append(m)

    if len(good) < 4:
        return None, len(good), 0, None, None

    src_pts = np.float32([kp_mac[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp_wf[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    H_resized, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0, maxIters=5000, confidence=0.995)
    if H_resized is None or mask is None:
        return None, len(good), 0, None, None

    inlier_mask = mask.ravel().astype(bool)
    num_inliers = int(inlier_mask.sum())

    # Composer avec la pré-échelle pour ramener H au repère original du maculaire
    S = np.diag([pre_scale, pre_scale, 1.0]).astype(np.float64)
    H = H_resized @ S

    # Erreur de reprojection (sur les inliers, dans le repère redimensionné)
    src_in = src_pts[inlier_mask].reshape(-1, 2)
    dst_in = dst_pts[inlier_mask].reshape(-1, 2)
    src_h = np.hstack([src_in, np.ones((src_in.shape[0], 1), dtype=np.float32)])
    proj = (H_resized @ src_h.T).T
    proj_xy = proj[:, :2] / np.maximum(proj[:, 2:3], 1e-9)
    errors = np.linalg.norm(proj_xy - dst_in, axis=1)
    reproj = float(np.median(errors)) if errors.size else None

    A = H[:2, :2]
    det = float(np.linalg.det(A))
    estimated_scale = float(np.sqrt(abs(det))) if det > 0 else None

    return H, len(good), num_inliers, estimated_scale, reproj


def register_pair(
    macular: FundusImage,
    widefield: FundusImage,
    *,
    expected_scale: float = 0.5,
    scale_tolerance: float = 0.5,
    min_inliers: int = 12,
    ratio_test: float = 0.78,
    pre_scales: tuple[float, ...] = (1.0, 0.6, 0.4, 0.25),
    detectors: tuple[str, ...] = ("akaze", "sift"),
) -> RegistrationResult:
    """Estime l'homographie projetant le maculaire dans le repère du widefield.

    Stratégie : on essaie plusieurs détecteurs et plusieurs pré-échelles du
    maculaire (pour couvrir des zooms macro très différents), et on garde
    la meilleure tentative au sens du nombre d'inliers RANSAC.
    """
    best = None
    best_score = -1
    best_pre = 1.0
    for det_kind in detectors:
        for ps in pre_scales:
            H, n_matches, n_inliers, scl, err = _attempt_register(
                macular.image, widefield.image, ps, det_kind, ratio_test,
            )
            if H is None:
                continue
            score = n_inliers
            if score > best_score:
                best_score = score
                best = (H, n_matches, n_inliers, scl, err)
                best_pre = ps

    if best is None:
        return RegistrationResult(
            macular_path=macular.path, widefield_path=widefield.path,
            homography=None, num_matches=0, num_inliers=0, inlier_ratio=0.0,
            estimated_scale=None, score=0.0, reprojection_error=None,
            accepted=False, reason="aucune tentative de recalage n'a abouti",
        )

    H, num_matches, num_inliers, estimated_scale, reprojection_error = best
    inlier_ratio = num_inliers / max(num_matches, 1)
    logger.debug("    meilleure pré-échelle = %.2f (inliers=%d)", best_pre, num_inliers)

    accepted = True
    reason = ""
    if num_inliers < min_inliers:
        accepted = False
        reason = f"inliers insuffisants ({num_inliers} < {min_inliers})"
    elif estimated_scale is None:
        accepted = False
        reason = "échelle non estimable"
    else:
        lo = (1 - scale_tolerance) * expected_scale
        hi = (1 + scale_tolerance) * expected_scale
        if not (lo <= estimated_scale <= hi):
            accepted = False
            reason = f"échelle hors plage ({estimated_scale:.2f}, attendu {expected_scale:.2f}±{scale_tolerance*100:.0f}%)"

    # Score combiné : inliers pondérés par la qualité de reprojection
    if reprojection_error is not None and reprojection_error > 0:
        score = num_inliers / (1.0 + reprojection_error)
    else:
        score = float(num_inliers)

    return RegistrationResult(
        macular_path=macular.path,
        widefield_path=widefield.path,
        homography=H,
        num_matches=num_matches,
        num_inliers=num_inliers,
        inlier_ratio=inlier_ratio,
        estimated_scale=estimated_scale,
        score=score,
        reprojection_error=reprojection_error,
        accepted=accepted,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Appariement de chaque maculaire avec le meilleur widefield
# ---------------------------------------------------------------------------

@dataclass
class MatchGroup:
    """Toutes les images d'un même patient et d'une même latéralité."""
    patient_id: str
    laterality: str
    source_folder: str = ""   # dossier de plus haut niveau (sous input_dir)
    maculars: list[FundusImage] = field(default_factory=list)
    widefields: list[FundusImage] = field(default_factory=list)


def _top_level_folder(file_path: Path, base_dir: Path) -> str:
    """Retourne le nom du dossier qui contient directement le fichier DICOM.

    On utilise le parent immédiat (pas le dossier de plus haut niveau sous
    base_dir), ce qui correspond mieux au "dossier de l'examen" tel que
    l'utilisateur le voit (ex. `Set_13042023` plutôt que `Set1`).
    Si le fichier est directement dans base_dir, retourne "".
    """
    try:
        rel = file_path.relative_to(base_dir)
    except ValueError:
        return file_path.parent.name
    parts = rel.parts
    if len(parts) <= 1:
        return ""
    return parts[-2]  # parent immédiat du fichier


def collect_groups(input_dir: Path) -> list[MatchGroup]:
    """Charge tous les DICOM fundus et les regroupe par (dossier source, patient, latéralité).

    On inclut le dossier source de plus haut niveau dans la clé : deux patients
    homonymes dans deux dossiers différents donnent deux groupes distincts.
    """
    files = sorted(p for p in input_dir.rglob("*") if p.suffix.lower() in (".dcm", ".dicom"))
    groups: dict[tuple[str, str, str], MatchGroup] = {}
    for f in files:
        # Skip rapide pour éviter d'ouvrir les non-fundus
        n = f.name.lower()
        if "fundus_macular" not in n and "fundus_widefield" not in n and "macular" not in n and "widefield" not in n:
            # On essaie quand même les autres si rien dans le nom (tag-only)
            if "fundus" not in n:
                continue

        fi = load_fundus_dicom(f)
        if fi is None:
            continue
        if fi.image_type not in ("fundus_macular", "fundus_widefield"):
            continue

        src = _top_level_folder(f, input_dir)
        # Clé : préférer patient_id du tag, sinon le nom du dossier parent
        pid = fi.patient_id or f.parent.name
        key = (src, pid, fi.laterality or "?")
        grp = groups.setdefault(key, MatchGroup(
            patient_id=pid, laterality=fi.laterality or "?", source_folder=src,
        ))
        if fi.image_type == "fundus_macular":
            grp.maculars.append(fi)
        else:
            grp.widefields.append(fi)

    return list(groups.values())


def match_group(group: MatchGroup, **register_kwargs) -> list[RegistrationResult]:
    """Pour chaque maculaire du groupe, retourne la meilleure paire avec un widefield.

    Si plusieurs widefields existent, on évalue chaque candidat et on garde
    celui qui maximise le score.
    """
    results: list[RegistrationResult] = []
    if not group.widefields or not group.maculars:
        print(
            f"Groupe (patient={group.patient_id}, lat={group.laterality}) ignoré: {len(group.maculars)} maculaire(s), {len(group.widefields)} widefield(s)"
        )
        return results

    for mac in group.maculars:
        candidates = [register_pair(mac, wf, **register_kwargs) for wf in group.widefields]
        # On prend le meilleur même s'il n'est pas accepté ; le rapport indiquera l'état
        candidates.sort(key=lambda r: r.score, reverse=True)
        best = candidates[0]
        results.append(best)
        print(
            f"  {mac.path.name} ↔ {best.widefield_path.name} : inliers={best.num_inliers}, échelle≈{best.estimated_scale or float("nan")}, err={best.reprojection_error or float("nan")}, accepté={best.accepted}"
        )
    return results


# ---------------------------------------------------------------------------
# Sorties / visualisations
# ---------------------------------------------------------------------------

def warp_macular_to_widefield(
    macular: np.ndarray, widefield_shape: tuple[int, int], H: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Warpe le maculaire dans le repère du widefield. Retourne (warped, mask)."""
    h, w = widefield_shape[:2]
    warped = cv2.warpPerspective(macular, H, (w, h), flags=cv2.INTER_LINEAR)
    mac_mask = np.full(macular.shape[:2], 255, dtype=np.uint8)
    warp_mask = cv2.warpPerspective(mac_mask, H, (w, h), flags=cv2.INTER_NEAREST)
    return warped, warp_mask


def make_overlay(widefield: np.ndarray, warped_macular: np.ndarray, mask: np.ndarray, alpha: float = 0.55) -> np.ndarray:
    """Superpose le maculaire warpé sur le widefield (alpha blending dans la zone du masque)."""
    if widefield.ndim == 2:
        widefield = cv2.cvtColor(widefield, cv2.COLOR_GRAY2RGB)
    if warped_macular.ndim == 2:
        warped_macular = cv2.cvtColor(warped_macular, cv2.COLOR_GRAY2RGB)

    out = widefield.copy()
    m3 = (mask > 0)[:, :, None]
    blended = (alpha * warped_macular.astype(np.float32) + (1 - alpha) * widefield.astype(np.float32)).astype(np.uint8)
    out = np.where(m3, blended, out)

    # Contour rouge de la zone maculaire pour la lisibilité
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(out, contours, -1, (255, 0, 0), thickness=4)
    return out


def make_side_by_side(widefield: np.ndarray, macular: np.ndarray, overlay: np.ndarray) -> np.ndarray:
    """Construit une vue widefield | maculaire | overlay, redimensionnée pour rester lisible."""
    target_h = 1024
    def _fit(img):
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        h, w = img.shape[:2]
        scale = target_h / h
        return cv2.resize(img, (int(w * scale), target_h))
    panels = [_fit(widefield), _fit(macular), _fit(overlay)]
    return np.hstack(panels)


def _to_bgr(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------------------
# Pipeline complet
# ---------------------------------------------------------------------------

def process_directory(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    expected_scale: float = 0.5,
    scale_tolerance: float = 0.4,
    min_inliers: int = 15,
    save_warped: bool = True,
    save_overlay: bool = True,
    save_side_by_side: bool = True,
) -> list[RegistrationResult]:
    """Traite un dossier de DICOM et produit un dossier de sortie par dossier source.

    Pour chaque sous-dossier de plus haut niveau dans `input_dir`, on crée un
    sous-dossier de sortie portant le même nom :

      output_dir/
        <folder_1>/
          pairs.csv          — paires de ce dossier source
          overlays/          — widefield + maculaire blendé
          warped/            — maculaire warpé seul
          side_by_side/      — vue tryptique
        <folder_2>/
          ...
        pairs.csv            — récapitulatif global (toutes paires)

    Si tous les fichiers sont à la racine de `input_dir` (pas de sous-dossier),
    la sortie est écrite directement dans `output_dir/<input_dir.name>/...`.
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    groups = collect_groups(input_dir)
    print(f"{len(groups)} groupe(s) (patient × œil) détecté(s) dans {input_dir}")

    # Regrouper les groupes par dossier source pour des sorties cloisonnées
    by_folder: dict[str, list[MatchGroup]] = {}
    for grp in groups:
        folder = grp.source_folder or input_dir.name
        by_folder.setdefault(folder, []).append(grp)

    all_results: list[RegistrationResult] = []

    for folder_name, folder_groups in by_folder.items():
        folder_out = output_dir / folder_name
        folder_out.mkdir(parents=True, exist_ok=True)
        print(f"→ Dossier source '{folder_name}' ({len(folder_groups)} groupe(s))")

        folder_results: list[RegistrationResult] = []

        for grp in folder_groups:
            print(
                f"  Groupe patient={grp.patient_id}, lat={grp.laterality} : {len(grp.maculars)} maculaire(s), {len(grp.widefields)} widefield(s)"
            )
            results = match_group(
                grp,
                expected_scale=expected_scale,
                scale_tolerance=scale_tolerance,
                min_inliers=min_inliers,
            )
            results = [r for r in results if r.accepted]
            folder_results.extend(results)

            if save_overlay:
                (folder_out / "overlays").mkdir(exist_ok=True)
            if save_warped:
                (folder_out / "warped").mkdir(exist_ok=True)
            if save_side_by_side:
                (folder_out / "side_by_side").mkdir(exist_ok=True)

            wf_cache = {wf.path: wf for wf in grp.widefields}
            mac_cache = {mac.path: mac for mac in grp.maculars}

            for res in results:
                if res.homography is None:
                    continue
                mac = mac_cache[res.macular_path]
                wf = wf_cache[res.widefield_path]
                warped, mask = warp_macular_to_widefield(mac.image, wf.image.shape, res.homography)

                base = res.macular_path.stem
                wf_tag = res.widefield_path.stem

                if save_warped:
                    cv2.imwrite(
                        str(folder_out / "warped" / f"{base}__warped_on__{wf_tag}.png"),
                        _to_bgr(warped),
                    )
                if save_overlay:
                    overlay = make_overlay(wf.image, warped, mask)
                    cv2.imwrite(
                        str(folder_out / "overlays" / f"{base}__overlay__{wf_tag}.png"),
                        _to_bgr(overlay),
                    )
                if save_side_by_side:
                    overlay = make_overlay(wf.image, warped, mask)
                    sbs = make_side_by_side(wf.image, mac.image, overlay)
                    cv2.imwrite(
                        str(folder_out / "side_by_side" / f"{base}__sbs__{wf_tag}.png"),
                        _to_bgr(sbs),
                    )

        _write_pairs_csv(folder_out / "pairs.csv", folder_results, input_dir)
        all_results.extend(folder_results)

    # Récapitulatif global à la racine de output_dir
    if len(by_folder) > 1:
        _write_pairs_csv(output_dir / "pairs.csv", all_results, input_dir)

    print(
        f"Recalage terminé : {len(all_results)} paire(s) traitée(s) dans {len(by_folder)} dossier(s) → {output_dir}"
    )
    return all_results


def _write_pairs_csv(path: Path, results: list[RegistrationResult], base_dir: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "macular", "widefield", "accepted", "reason",
            "num_matches", "num_inliers", "inlier_ratio",
            "estimated_scale", "reprojection_error_px", "score",
        ])
        for r in results:
            try:
                mac = r.macular_path.relative_to(base_dir)
            except ValueError:
                mac = r.macular_path
            try:
                wf = r.widefield_path.relative_to(base_dir)
            except ValueError:
                wf = r.widefield_path
            w.writerow([
                str(mac), str(wf), r.accepted, r.reason,
                r.num_matches, r.num_inliers, f"{r.inlier_ratio:.3f}",
                f"{r.estimated_scale:.4f}" if r.estimated_scale is not None else "",
                f"{r.reprojection_error:.2f}" if r.reprojection_error is not None else "",
                f"{r.score:.2f}",
            ])
