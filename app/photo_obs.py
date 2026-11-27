"""Private Huawei OBS transport for synchronized photos."""

import base64
import hashlib
import hmac
import re
from urllib.parse import urlsplit

import certifi

MAX_OBJECT_BYTES = 12 * 1024 * 1024


def object_key(site, parts):
    relative = parts[1:] if site == "cas" else parts
    if not relative:
        raise ValueError("Missing photo object path")
    if relative[0] == "label-photos":
        return "/".join([site, "label", *relative[1:]])
    return "/".join([site, "crops", *relative])


class ObsPhotoStore:
    def __init__(self, settings):
        from obs import ObsClient

        endpoint = urlsplit(settings.photo_obs_endpoint)
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
            raise ValueError("OBS endpoint must be an HTTPS origin")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", settings.photo_obs_bucket):
            raise ValueError("Invalid OBS bucket name")
        if not settings.photo_obs_access_key or not settings.photo_obs_secret_key:
            raise ValueError("OBS requires an access key and secret key")
        self.bucket = settings.photo_obs_bucket
        self.client = ObsClient(
            access_key_id=settings.photo_obs_access_key,
            secret_access_key=settings.photo_obs_secret_key,
            server=settings.photo_obs_endpoint,
            ssl_verify=certifi.where(),
            timeout=10,
            max_retry_count=0,
            max_redirect_count=0,
        )
        self.client.context.check_hostname = True

    def read(self, key):
        response = self.client.getObject(self.bucket, key, loadStreamInMemory=False)
        if response.status == 404:
            raise FileNotFoundError("Photo not found in OBS")
        if response.status != 200:
            raise OSError(f"OBS download failed ({response.status})")
        stream = response.body.response
        try:
            data = bytearray()
            while chunk := stream.read(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_OBJECT_BYTES:
                    raise OSError("OBS photo exceeds maximum size")
            return bytes(data)
        finally:
            stream.close()

    def upload(self, key, data, digest):
        from obs import PutObjectHeader

        try:
            existing = self.read(key)
        except FileNotFoundError:
            checksum = hashlib.md5(data, usedforsecurity=False).digest()
            headers = PutObjectHeader(md5=base64.b64encode(checksum).decode())
            response = self.client.putContent(
                self.bucket, key, data, metadata={"sha256": digest}, headers=headers,
                extensionHeaders={"x-obs-forbid-overwrite": "true"},
            )
            if response.status not in (200, 201, 409, 412):
                return "pending"
            existing = self.read(key)
        return "synced" if hmac.compare_digest(hashlib.sha256(existing).hexdigest(), digest) else "failed"
