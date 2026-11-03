"""Les images carrées (IR 8 bits décodées à tort comme OCT) ne doivent jamais sortir comme B-scans."""
import numpy as np

from pipeline_wp2.reader import _est_image_en_face, _volume_to_array


class _Vol:
    def __init__(self, tranches, volume_id="1_2_3"):
        self.volume = tranches
        self.volume_id = volume_id


def _bscan(h=496, w=768, v=0.5):
    return np.full((h, w), v, dtype=np.float32)


def test_est_image_en_face():
    assert _est_image_en_face(np.zeros((768, 768)))
    assert _est_image_en_face(np.zeros((512, 512)))
    assert not _est_image_en_face(np.zeros((496, 768)))
    assert not _est_image_en_face(np.zeros((496, 1536)))


def test_volume_avec_une_image_carree_garde_les_bscans():
    # avant correction : np.stack échouait et TOUT le volume était perdu
    vol = _Vol([_bscan(), _bscan(), np.zeros((768, 768), dtype=np.float32), _bscan()])
    arr = _volume_to_array(vol)
    assert arr is not None and arr.shape == (3, 496, 768)


def test_volume_entierement_carre_est_ignore():
    assert _volume_to_array(_Vol([np.zeros((768, 768), dtype=np.float32)])) is None
    assert _volume_to_array(_Vol([np.zeros((512, 512)), np.zeros((512, 512))])) is None


def test_volume_normal_inchange():
    arr = _volume_to_array(_Vol([_bscan(), _bscan(496, 768, 0.2)]))
    assert arr.shape == (2, 496, 768) and arr.dtype == np.uint8


def test_e2e_structure_examinee_vide_reste_lisible(tmp_path):
    """12 fichiers réels échouaient (IndexError dans read_all_metadata, bloc 9005 sans texte)
    alors que leurs images étaient intactes : la lecture doit aboutir."""
    from conftest import e2e_bytes
    from oct_converter.readers import E2E
    from pipeline_wp2.reader import read_e2e
    f = tmp_path / "x.e2e"
    f.write_bytes(e2e_bytes([dict(serie=1, motif="Line", lat="R", bscans=[(0, 0, 10, 0)], n_oct=1, n_fond=1,
                                  structure_vide=True)], pixels=True))
    try:  # l'échec d'origine doit bien être reproduit par oct_converter seul
        E2E(str(f)).read_oct_volume()
        reproduit = False
    except IndexError:
        reproduit = True
    assert reproduit
    res = read_e2e(f)
    assert [r.image_type for r in res].count("oct_bscan") == 1


def test_inventaire_bloc_ind1_8bits_n_est_pas_de_l_oct(tmp_path):
    """Une série dont les seuls blocs ind 1 sont à 8 bits (IR de repérage) n'est pas de l'OCT."""
    from conftest import e2e_bytes
    import analyse_contenu as ac
    f = tmp_path / "x.e2e"
    f.write_bytes(e2e_bytes([
        dict(serie=1, en_face="IR", lat="R", n_ind1_8bits=1),
        dict(serie=2, motif="Line", lat="R", bscans=[(0, 0, 10, 0)], n_oct=1, n_ind1_8bits=1),
    ], pixels=True))
    par_serie = {r["serie"]: r for r in ac.analyser(f, "E2E_HEIDELBERG", str(f))}
    assert par_serie["1_1"]["modalite"] == "infrarouge", par_serie["1_1"]
    assert par_serie["1_2"]["modalite"] == "oct_bscan", par_serie["1_2"]
