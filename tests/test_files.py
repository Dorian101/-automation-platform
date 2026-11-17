import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.core.identity import WEB, Identity
from app.core.results import CommandError
from app.db.passkeys_repo import PasskeysRepository
from app.plugins import files as files_module
from app.plugins.files import FilesPlugin, clean_name, sniff
from app.plugins.files.web import MISSES_PER_HOUR, disposition
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, with_passkey

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
OWNER = Identity(kind=WEB, id=TEST_USERNAME)


@pytest.fixture
def plugin(database_with_schema, tmp_path):
    return FilesPlugin(database_with_schema, folder=tmp_path)


@pytest.fixture
async def client(manager, plugin, database_with_schema, web_server_factory):
    manager.register(plugin)
    server = web_server_factory(manager, database_with_schema)
    made = TestClient(TestServer(server.build_app()))
    await made.start_server()
    yield made
    await made.close()


@pytest.fixture
async def owner(client, login, create_user):
    await login(client)
    return client


@pytest.fixture
async def admin(owner, users):
    account = users.get_by_username(TEST_USERNAME)
    users.set_admin(account.id, True)
    PasskeysRepository(users.database).add(account.id, b"key", b"public", 0, "test")
    with_passkey(owner, users)
    return owner


async def _upload(client, name="shot.png", body=PNG):
    return await client.post("/files/upload", data=body, headers={"X-File-Name": name})


async def _chunks(*parts):
    for part in parts:
        yield part


async def _share(plugin, ids) -> str:
    view = await plugin.page_action(OWNER, "share", {"ids": ",".join(map(str, ids))})
    url = next(t for t in view["list"] if t["title"].startswith("Новая"))["rows"][0][
        "url"
    ]
    return url[url.index("/s/") :]


class TestHelpers:
    def test_sniff_reads_the_content_not_the_name(self):
        assert sniff(PNG[:16]) == "image/png"
        assert sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 12) == "image/jpeg"
        assert sniff(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4) == "video/mp4"
        assert sniff(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 4) == ""
        assert sniff(b"<svg xmlns='http://www.w3.org/2000/svg'>") == ""
        assert sniff(b"<html><script>") == ""

    def test_clean_name_drops_paths_and_control_characters(self):
        assert clean_name("../../etc/passwd") == "passwd"
        assert clean_name("C:\\Users\\me\\a\x00b.png") == "ab.png"
        assert clean_name("   ") == "файл"
        long = clean_name("я" * 300 + ".jpg")
        assert len(long) == 200 and long.endswith(".jpg")

    def test_disposition_keeps_quotes_out_of_the_header(self):
        header = disposition("attachment", 'a"b\\c\r\nd фото.png')
        assert "\r" not in header and "\n" not in header
        assert 'filename="abcd .png"' in header
        assert "filename*=UTF-8''a%22b%5Cc%0D%0Ad%20" in header


class TestUpload:
    async def test_a_signed_in_user_uploads_and_downloads(
        self, owner, plugin, tmp_path
    ):
        response = await _upload(owner, "отчёт.png")
        assert response.status == 200, await response.text()
        file_id = (await response.json())["id"]

        assert len(list(tmp_path.iterdir())) == 1
        view = await plugin.page_view(OWNER)
        assert view["list"][0]["rows"][0]["name"] == "отчёт.png"

        got = await owner.get(f"/files/get/{file_id}")
        assert got.status == 200
        assert await got.read() == PNG
        assert got.headers["Content-Type"] == "application/octet-stream"
        assert got.headers["Content-Disposition"].startswith("attachment;")

    async def test_a_guest_cannot_upload(self, client, tmp_path):
        response = await client.post(
            "/files/upload",
            data=PNG,
            headers={"X-File-Name": "a.png"},
            allow_redirects=False,
        )
        assert response.status != 200
        assert not tmp_path.exists() or not list(tmp_path.iterdir())

    async def test_an_empty_or_nameless_upload_is_refused(self, owner):
        assert (await _upload(owner, body=b"")).status == 400
        assert (await owner.post("/files/upload", data=PNG)).status == 400

    async def test_the_size_limits(self, owner, monkeypatch):
        monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 10)
        assert (await _upload(owner)).status == 400

        monkeypatch.setattr(files_module, "MAX_FILE_BYTES", 10_000)
        monkeypatch.setattr(files_module, "TOTAL_BYTES", len(PNG) + 10)
        assert (await _upload(owner)).status == 200
        refused = await _upload(owner)
        assert refused.status == 400
        assert "Место" in (await refused.json())["error"]

    async def test_more_bytes_than_declared_leave_nothing_behind(
        self, plugin, tmp_path
    ):
        with pytest.raises(CommandError):
            await plugin.store(OWNER, "a.png", 4, _chunks(b"12", b"345"))
        with pytest.raises(CommandError):
            await plugin.store(OWNER, "a.png", 9, _chunks(b"12"))

        assert list(tmp_path.iterdir()) == []
        assert plugin.repo.used_bytes() == 0

    async def test_another_user_cannot_download(
        self, client, login, create_user, plugin
    ):
        entry = await plugin.store(OWNER, "a.png", len(PNG), _chunks(PNG))
        create_user(username="hr", password=TEST_PASSWORD)
        await login(client, username="hr")

        assert (await client.get(f"/files/get/{entry['id']}")).status == 404

    async def test_startup_removes_only_its_own_orphans(self, plugin, tmp_path):
        (tmp_path / ("a" * 32)).write_bytes(b"x")
        (tmp_path / ("b" * 32 + ".part")).write_bytes(b"x")
        (tmp_path / "backup.sql").write_bytes(b"x")

        await plugin.on_startup()
        await plugin.on_shutdown()

        assert [p.name for p in tmp_path.iterdir()] == ["backup.sql"]

    async def test_delete_removes_the_file_from_disk(self, plugin, tmp_path):
        entry = await plugin.store(OWNER, "a.png", len(PNG), _chunks(PNG))
        await plugin.page_action(OWNER, "delete", {"id": str(entry["id"])})

        assert list(tmp_path.iterdir()) == []
        assert (await plugin.page_view(OWNER))["list"][0]["rows"] == []


class TestShare:
    async def test_only_an_admin_with_a_passkey_creates_links(self, owner, plugin):
        entry = await plugin.store(OWNER, "a.png", len(PNG), _chunks(PNG))

        with pytest.raises(CommandError):
            await plugin.page_action(OWNER, "share", {"ids": str(entry["id"])})

    async def test_someone_else_s_file_cannot_be_shared(self, admin, plugin):
        other = Identity(kind=WEB, id="hr")
        entry = await plugin.store(other, "a.png", len(PNG), _chunks(PNG))

        with pytest.raises(CommandError):
            await _share(plugin, [entry["id"]])

    async def test_a_link_shows_and_serves_only_its_files(self, admin, plugin):
        shown = await plugin.store(OWNER, "<i>x<i>.png", len(PNG), _chunks(PNG))
        other = await plugin.store(OWNER, "notes.txt", 5, _chunks(b"hello"))
        path = await _share(plugin, [shown["id"]])
        admin.session.cookie_jar.clear()

        page = await admin.get(path)
        body = await page.text()
        assert page.status == 200
        assert "&lt;i&gt;x&lt;i&gt;.png" in body and "<i>x" not in body
        assert "notes.txt" not in body
        assert "sandbox" not in page.headers["Content-Security-Policy"]
        assert "script-src" not in page.headers["Content-Security-Policy"]
        assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert "<script" not in body

        download = await admin.get(f"{path}/{shown['id']}")
        assert download.status == 200
        assert download.headers["Content-Disposition"].startswith("attachment;")
        assert "sandbox" in download.headers["Content-Security-Policy"]

        preview = await admin.get(f"{path}/{shown['id']}?view=1")
        assert preview.headers["Content-Type"] == "image/png"
        assert preview.headers["Content-Disposition"].startswith("inline;")

        assert (await admin.get(f"{path}/{other['id']}")).status == 404

    async def test_text_is_never_shown_inline(self, admin, plugin):
        entry = await plugin.store(OWNER, "x.html", 14, _chunks(b"<html><script>"))
        path = await _share(plugin, [entry["id"]])

        got = await admin.get(f"{path}/{entry['id']}?view=1")
        assert got.headers["Content-Type"] == "application/octet-stream"
        assert got.headers["Content-Disposition"].startswith("attachment;")

    async def test_the_token_is_not_stored(self, admin, plugin, database_with_schema):
        entry = await plugin.store(OWNER, "a.png", len(PNG), _chunks(PNG))
        token = (await _share(plugin, [entry["id"]]))[3:]

        with database_with_schema.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM shares")
            stored = cur.fetchone()[0]
        assert token not in stored and len(stored) == 64

    async def test_revoked_deleted_and_expired_links_are_gone(
        self, admin, plugin, database_with_schema, tmp_path
    ):
        first = await plugin.store(OWNER, "a.png", len(PNG), _chunks(PNG))
        revoked = await _share(plugin, [first["id"]])
        share_id = plugin.repo.shares([OWNER])[0]["id"]
        await plugin.page_action(OWNER, "revoke", {"id": str(share_id)})

        second = await plugin.store(OWNER, "b.png", len(PNG), _chunks(PNG))
        deleted = await _share(plugin, [second["id"]])
        await plugin.page_action(OWNER, "delete", {"id": str(second["id"])})

        third = await plugin.store(OWNER, "c.png", len(PNG), _chunks(PNG))
        expired = await _share(plugin, [third["id"]])
        with database_with_schema.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE files SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 minute' "
                "WHERE id = %s",
                (third["id"],),
            )
            conn.commit()

        for path in (revoked, deleted, expired):
            assert (await admin.get(path)).status == 404, path

        plugin.sweep()
        assert sorted(p.name for p in tmp_path.iterdir()) == [first["stored"]]
        assert plugin.repo.shares([OWNER]) == []

    async def test_guessing_links_is_rate_limited(self, client):
        statuses = [
            (await client.get("/s/" + "A" * 43)).status
            for _ in range(MISSES_PER_HOUR + 1)
        ]

        assert statuses[:MISSES_PER_HOUR] == [404] * MISSES_PER_HOUR
        assert statuses[-1] == 429

    async def test_a_switched_off_plugin_serves_nothing(self, admin, plugin, manager):
        entry = await plugin.store(OWNER, "a.png", len(PNG), _chunks(PNG))
        path = await _share(plugin, [entry["id"]])

        class Off:
            def disabled_plugins(self):
                return {"files"}

            def maintenance(self):
                return False

        switches = manager.switches
        manager.switches = Off()
        try:
            assert (await admin.get(path)).status == 404
            assert (await _upload(admin)).status == 404
        finally:
            manager.switches = switches


def test_the_link_token_pattern_matches_what_is_minted():
    import secrets

    assert re.fullmatch(files_module.TOKEN, secrets.token_urlsafe(32))
