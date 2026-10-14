import pytest

from app.core.passwords import (
    ALGORITHM,
    PasswordError,
    hash_password,
    verify_password,
)

PASSWORD = "correct-horse-battery"


class TestHashing:
    def test_records_the_algorithm_and_its_parameters(self):
        """Whatever the module is configured for is what goes in the hash.

        Compared against the constants rather than against a number, because
        that is what the assertion is for: the parameters are *recorded*. The
        cost they are worth is the next test's business.
        """
        import app.core.passwords as passwords

        stored = hash_password(PASSWORD)
        algorithm, n, r, p, salt, digest = stored.split("$")

        assert algorithm == ALGORITHM
        assert int(n) == passwords.SCRYPT_N
        assert int(r) == passwords.SCRYPT_R
        assert int(p) == passwords.SCRYPT_P
        assert salt and digest

    def test_meets_the_owasp_cost(self, shipped_scrypt_cost):
        """OWASP's scrypt guidance calls for at least 2^17.

        Read off the constant rather than out of a hash. Everything else here
        runs at the cheap cost that conftest sets, and a hash made there would
        only report what the fixture chose — which would turn this into a test
        of the fixture.
        """
        n, r = shipped_scrypt_cost

        assert n >= 2**17
        assert r >= 8


class TestTheShippedCost:
    """The one place that pays the real price.

    Everywhere else in the suite the cost is lowered so that the tests are not
    a measurement of the password module. That leaves a gap: nothing would
    notice if the shipped parameters no longer fit in the memory `_derive`
    allows, and the failure would be a `MemoryError` in production and on
    nobody's laptop. So the configuration this replaces is checked once, here,
    by actually running it.
    """

    def test_it_hashes_and_verifies_at_the_shipped_cost(
        self, monkeypatch, shipped_scrypt_cost
    ):
        import app.core.passwords as passwords
        from tests.conftest import TEST_SCRYPT_N

        shipped = shipped_scrypt_cost[0]
        monkeypatch.setattr(passwords, "SCRYPT_N", shipped)
        stored = hash_password(PASSWORD)

        assert int(stored.split("$")[1]) == shipped
        assert verify_password(PASSWORD, stored) is True
        assert verify_password("not-the-password", stored) is False
        # Otherwise the test proves nothing: it would be measuring the cheap
        # cost and calling it the shipped one.
        assert TEST_SCRYPT_N < shipped

    def test_hashes_made_at_the_old_cost_still_verify(self, monkeypatch):
        """A cost bump must not invalidate every stored hash.

        The parameters are recorded per password, so raising the default
        leaves old hashes verifiable without a one-off migration.
        """
        import app.core.passwords as passwords

        monkeypatch.setattr(passwords, "SCRYPT_N", 2**14)
        old = hash_password(PASSWORD)

        monkeypatch.setattr(passwords, "SCRYPT_N", 2**17)

        assert verify_password(PASSWORD, old) is True

    def test_is_salted(self):
        """Two hashes of one password must not be comparable."""
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_does_not_contain_the_password(self):
        assert PASSWORD not in hash_password(PASSWORD)

    def test_round_trips(self):
        assert verify_password(PASSWORD, hash_password(PASSWORD)) is True

    def test_rejects_a_wrong_password(self):
        assert verify_password("not-the-password", hash_password(PASSWORD)) is False

    def test_rejects_a_short_password(self):
        with pytest.raises(PasswordError, match="не короче"):
            hash_password("short")


class TestVerificationAgainstBadInput:
    """A corrupt hash must fail the login, not crash it."""

    @pytest.mark.parametrize(
        "stored",
        [
            "",
            "nonsense",
            "bcrypt$16384$8$1$c2FsdA==$ZGlnZXN0",
            "scrypt$16384$8$1$not-base64!!$ZGlnZXN0",
            "scrypt$abc$8$1$c2FsdA==$ZGlnZXN0",
            "scrypt$16384$8$1$c2FsdA==",
            "scrypt$16384$8$1$$",
            "scrypt$16384$8$1$YQ==$YQ==",
        ],
    )
    def test_returns_false(self, stored):
        assert verify_password(PASSWORD, stored) is False

    def test_does_not_raise_on_missing_fields(self):
        assert verify_password(PASSWORD, "scrypt$only$two") is False

    def test_plain_password_stored_by_mistake_is_not_accepted(self):
        """Nothing in the codebase may ever write a raw password."""
        assert verify_password(PASSWORD, PASSWORD) is False
