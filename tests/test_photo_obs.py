import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.photo_obs import ObsPhotoStore, object_key
from app.photo_sync import PhotoSync


@pytest.mark.parametrize(("site", "ref", "expected"), [
    ("bgd", "batch/seq_1_top.png", "bgd/crops/batch/seq_1_top.png"),
    ("cas", "cas/batch/seq_1_side.png", "cas/crops/batch/seq_1_side.png"),
    ("bgd", "label-photos/a.jpg", "bgd/label/a.jpg"),
    ("cas", "cas/label-photos/a.jpg", "cas/label/a.jpg"),
])
def test_obs_keys_keep_warehouse_and_photo_type(site, ref, expected):
    assert object_key(site, PhotoSync._parts(site, ref)) == expected


def test_obs_upload_checks_bytes_and_never_overwrites_existing_object():
    obs = ObsPhotoStore.__new__(ObsPhotoStore)
    obs.bucket = "bucket-cas-bgd"
    obs.client = Mock()
    data = b"photo bytes"
    digest = hashlib.sha256(data).hexdigest()
    key = "bgd/crops/batch/seq_1_top.png"
    obs.client.getObject.side_effect = [
        SimpleNamespace(status=404),
        SimpleNamespace(status=200, body=SimpleNamespace(response=BytesIO(data))),
        SimpleNamespace(status=200, body=SimpleNamespace(response=BytesIO(b"different bytes"))),
    ]
    obs.client.putContent.return_value = SimpleNamespace(status=200)

    assert obs.upload(key, data, digest) == "synced"
    assert obs.client.putContent.call_args.kwargs["extensionHeaders"] == {"x-obs-forbid-overwrite": "true"}
    assert obs.upload(key, data, digest) == "failed"
    obs.client.putContent.assert_called_once()
