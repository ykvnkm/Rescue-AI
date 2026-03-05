class S3ArtifactStore:
    """S3-compatible artifact store stub for future SDK integration."""

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        self._endpoint_url = endpoint_url.rstrip("/")
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key

    def upload_file(self, local_path: str, remote_key: str) -> str:
        # Network upload is intentionally not implemented in scaffold mode.
        _ = local_path, self._access_key, self._secret_key
        return f"{self._endpoint_url}/{self._bucket}/{remote_key.lstrip('/')}"

