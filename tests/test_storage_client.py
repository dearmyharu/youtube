from src.storage_client import StorageError, SupabaseStorageClient, save_thumbnail


class FakeResponse:
    def __init__(self, status_code=200, text="", content=b"", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, get_response=None, post_response=None):
        self._get_response = get_response
        self._post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get_response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post_response


class TestSupabaseStorageClient:
    def test_public_url_format(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret", bucket="thumbnails")
        assert client.public_url("v1.jpg") == "https://abc.supabase.co/storage/v1/object/public/thumbnails/v1.jpg"

    def test_base_url_strips_trailing_slash(self):
        client = SupabaseStorageClient("https://abc.supabase.co/", "secret")
        assert client.base == "https://abc.supabase.co/storage/v1"

    def test_headers_include_bearer_and_apikey(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        headers = client._headers()
        assert headers["Authorization"] == "Bearer secret"
        assert headers["apikey"] == "secret"

    def test_ensure_bucket_success(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        client.session = FakeSession(post_response=FakeResponse(status_code=200))
        client.ensure_bucket()  # should not raise

    def test_ensure_bucket_already_exists_is_ok(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        client.session = FakeSession(post_response=FakeResponse(status_code=409, text="Bucket already exists"))
        client.ensure_bucket()  # should not raise

    def test_ensure_bucket_other_error_raises(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        client.session = FakeSession(post_response=FakeResponse(status_code=500, text="boom"))
        try:
            client.ensure_bucket()
            assert False, "expected StorageError"
        except StorageError:
            pass

    def test_upload_success_returns_path(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        client.session = FakeSession(post_response=FakeResponse(status_code=200))
        assert client.upload("v1.jpg", b"data") == "v1.jpg"

    def test_upload_failure_raises(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        client.session = FakeSession(post_response=FakeResponse(status_code=403, text="forbidden"))
        try:
            client.upload("v1.jpg", b"data")
            assert False, "expected StorageError"
        except StorageError:
            pass


class TestSaveThumbnail:
    def test_picks_jpg_extension_by_default(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        client.session = FakeSession(post_response=FakeResponse(status_code=200))
        download_session = FakeSession(get_response=FakeResponse(status_code=200, content=b"imgdata",
                                                                   headers={"Content-Type": "image/jpeg"}))
        path = save_thumbnail(client, "vid1", "http://example.com/thumb.jpg", session=download_session)
        assert path == "vid1.jpg"

    def test_picks_png_extension_when_content_type_is_png(self):
        client = SupabaseStorageClient("https://abc.supabase.co", "secret")
        client.session = FakeSession(post_response=FakeResponse(status_code=200))
        download_session = FakeSession(get_response=FakeResponse(status_code=200, content=b"imgdata",
                                                                   headers={"Content-Type": "image/png"}))
        path = save_thumbnail(client, "vid1", "http://example.com/thumb.png", session=download_session)
        assert path == "vid1.png"
