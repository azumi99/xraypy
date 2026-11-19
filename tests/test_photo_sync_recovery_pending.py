import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import DedupConfig, Settings
from app.dedup.sqlite_store import SqliteStore
from app.geometry import encode_png
from app.photo_sync import PhotoSync, is_final_png, is_pending_png
from app.pipeline import _save_capture, _save_original
from app.warehouse import warehouse_context


def test_pending_recovery_and_legacy(tmp_path):
    async def run():
        settings = Settings(_env_file=None, sqlite_path=str(tmp_path / "db"),
                            crop_dir=tmp_path / "photos", dedup=DedupConfig(store="sqlite"))
        store = SqliteStore(settings)
        await store.initialize()
        sync = PhotoSync(SimpleNamespace(photo_sync_enabled=True, crop_dir=settings.crop_dir,
                         photo_sync_outbox_path=tmp_path / "outbox", photo_sync_bgd_token="b",
                         photo_sync_cas_token="c", photo_sync_url="https://example.test"), store)
        image = np.full((20, 200, 4), 123, dtype=np.uint8)
        for site in ("bgd", "cas"):
            token = warehouse_context.set(site)
            try:
                prefix = "cas/" if site == "cas" else ""
                top, side, legacy = [prefix + "batch/" + name + ".png" for name in ("top", "side", "legacy")]
                awb = await store.insert_awb("MAWB-" + site, hawb_number="HAWB", part=2, total=3)
                conn = store._connect()
                conn.execute("INSERT INTO parcel_identity (id,batch_id,track_id,status,model_name,model_version,crop_ref,crop_ref_side,awb_id,created_at) VALUES (1,'batch',1,'NEW','m','v',?,?,?, 'now')", (top, side, awb.id))
                conn.execute("UPDATE awb SET status='PAIRED',parcel_id=1 WHERE id=?", (awb.id,))
                conn.execute("INSERT INTO parcel_identity (id,batch_id,track_id,status,model_name,model_version,crop_ref,created_at) VALUES (2,'batch',2,'REVIEW','m','v',?, 'now')", (legacy,))
                conn.commit()
                for ref in (top, side):
                    _save_original(settings.crop_dir / ref, image)
                    assert is_pending_png((settings.crop_dir / ref).read_bytes())
                orphan = settings.crop_dir / prefix / "batch/orphan_side.png"
                _save_original(orphan, image)
                (settings.crop_dir / legacy).write_bytes(encode_png(image))
                label = prefix + "label-photos/qr.png"
                label_path = settings.crop_dir / label
                label_path.parent.mkdir(parents=True, exist_ok=True)
                label_path.write_bytes(encode_png(image))
                conn.execute("UPDATE awb SET label_photo_ref=? WHERE id=?", (label, awb.id))
                conn.commit()
            finally:
                warehouse_context.reset(token)
        await sync.reconcile()
        for site in ("bgd", "cas"):
            prefix = "cas/" if site == "cas" else ""
            for view in ("top", "side"):
                path = settings.crop_dir / prefix / "batch" / (view + ".png")
                expected = tmp_path / (site + view + ".png")
                _save_capture(expected, image, view, SimpleNamespace(awb_number="MAWB-" + site,
                              hawb_number="HAWB", part=2, total=3))
                assert path.read_bytes() == expected.read_bytes()
                assert is_final_png(path.read_bytes())
                assert sync.status(site, [prefix + "batch/" + view + ".png"])[prefix + "batch/" + view + ".png"] == "pending"
            assert (settings.crop_dir / prefix / "batch/legacy.png").read_bytes() == encode_png(image)
            assert is_pending_png((settings.crop_dir / prefix / "batch/orphan_side.png").read_bytes())
            assert sync.status(site, [prefix + "label-photos/qr.png"])[prefix + "label-photos/qr.png"] == "pending"
        await store.close()
    asyncio.run(run())


def test_competing_finalizers_do_not_overwrite(tmp_path):
    image = np.zeros((10, 100, 4), dtype=np.uint8)
    path = tmp_path / "capture.png"
    owned = _save_original(path, image)
    _save_capture(path, image, "top", owned=owned)
    final = path.read_bytes()
    with pytest.raises((FileExistsError, ValueError)):
        _save_capture(path, image, "side", owned=owned)
    assert path.read_bytes() == final


def test_concurrent_finalizers(tmp_path):
    image = np.zeros((10, 100, 4), dtype=np.uint8)
    path = tmp_path / "race.png"
    owned = _save_original(path, image)

    def finalize(view):
        try:
            _save_capture(path, image, view, owned=owned)
            return True
        except (FileExistsError, ValueError):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finalize, ("top", "side")))
    assert sorted(results) == [False, True]
    assert is_final_png(path.read_bytes())
