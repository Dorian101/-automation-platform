import pytest

from app.core.identity import WEB, Identity, InvalidIdentity, telegram


class TestIdentity:
    def test_telegram_helper(self):
        assert str(telegram(-100123)) == "telegram:-100123"

    def test_str_roundtrip(self):
        original = Identity(kind=WEB, id="default")
        assert Identity.parse(str(original)) == original

    def test_namespacing_prevents_collision(self):
        tg = Identity(kind="telegram", id="777")
        web_user = Identity(kind=WEB, id="777")

        assert str(tg) != str(web_user)

    @pytest.mark.parametrize("value", ["", "nocolon", ":missing-kind", "missing-id:"])
    def test_parse_rejects_malformed(self, value):
        with pytest.raises(InvalidIdentity):
            Identity.parse(value)

    @pytest.mark.parametrize("kwargs", [
        {"kind": "", "id": "x"},
        {"kind": "web", "id": ""},
    ])
    def test_rejects_empty_parts(self, kwargs):
        with pytest.raises(InvalidIdentity):
            Identity(**kwargs)

    def test_is_hashable(self):
        identity = Identity(kind=WEB, id="default")
        assert {identity, Identity(kind=WEB, id="default")} == {identity}
