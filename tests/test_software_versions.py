"""SoftwareVersions est multivaluée sur les Optos (7 valeurs) ; elle ne doit pas être convertie en une
seule chaîne « ['a', 'b', ...] » de plus de 64 caractères (VR LO non conforme)."""
import numpy as np
import pydicom
from pydicom.dataset import Dataset

from pipeline_wp2 import reader
from pipeline_wp2.writer import write_processed_dicom

OPTOS = [f"composant{i}=1.2.34.567{i}" for i in range(7)]  # 7 valeurs de moins de 64 caractères


def _meta(valeur):
    ds = Dataset()
    ds.Manufacturer = "OPTOS"
    ds.SoftwareVersions = valeur
    return reader._extract_dicom_metadata(ds)


def _ecrire(tmp_path, version_logicielle):
    meta = {"source_file": "x.dcm", "manufacturer": "OPTOS", "software_version": version_logicielle}
    image = np.zeros((32, 32, 3), np.uint8)
    chemin = write_processed_dicom(image, meta, "R", None, None, None, tmp_path / "s.dcm")
    return pydicom.dcmread(str(chemin))


def test_lecture_conserve_les_valeurs_separees():
    assert _meta(OPTOS)["software_version"].split("\\") == OPTOS
    assert _meta("1.1.4.600")["software_version"] == "1.1.4.600"


def test_ecriture_multivaluee_et_conforme(tmp_path):
    ds = _ecrire(tmp_path, "\\".join(OPTOS))
    assert list(ds.SoftwareVersions) == OPTOS
    assert all(len(v) <= 64 for v in ds.SoftwareVersions)


def test_valeur_trop_longue_tronquee(tmp_path):
    ds = _ecrire(tmp_path, "x" * 130)
    assert len(str(ds.SoftwareVersions)) == 64
