"""Photo sync must not run before captures have verifiable final contents."""


class PhotoSyncService:
    def __init__(self, settings):
        raise RuntimeError("Photo sync is disabled pending safe photo verification")
