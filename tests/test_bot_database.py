"""Bot-side database.py functions, run against a scripted fake Supabase client.

FakeSupabase.responses is a queue: each .execute() pops the next entry (rows, or an Exception
to raise). Every call chain is logged, so tests can check exactly what was sent.
"""

from types import SimpleNamespace

import pytest

from src import database


class Result:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.ops = []

    def __getattr__(self, name):                 # select / insert / update / delete / eq / in_ ...
        def method(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return method

    def execute(self):
        self.client.log.append((self.table, self.ops))
        response = self.client.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Result(response)

    def op(self, name):
        return [(args, kwargs) for n, args, kwargs in self.ops if n == name]


class FakeAdmin:
    def __init__(self, client):
        self.client = client

    def create_user(self, attributes):
        self.client.auth_calls.append(("create_user", attributes))
        if self.client.create_error:
            raise self.client.create_error
        return SimpleNamespace(user=SimpleNamespace(id="auth-1"))

    def delete_user(self, user_id):
        self.client.auth_calls.append(("delete_user", user_id))
        if self.client.delete_error:
            raise self.client.delete_error


class FakeSupabase:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.log = []
        self.auth_calls = []
        self.create_error = None
        self.delete_error = None
        self.auth = SimpleNamespace(admin=FakeAdmin(self))

    def table(self, name):
        return FakeQuery(self, name)


class UniqueViolation(Exception):
    code = "23505"


@pytest.fixture
def fake(monkeypatch):
    def install(*responses):
        client = FakeSupabase(responses)
        monkeypatch.setattr(database, "supabase", client)
        return client
    return install


def sent(client, index):
    """(table, ops) of the index-th database call."""
    return client.log[index]


def ops_of(client, index, name):
    table, ops = client.log[index]
    return [(a, k) for n, a, k in ops if n == name]


# ------------------------------------------------------------------ fetch_subscribers (scraper side)

def test_fetch_subscribers_skips_paused_users(fake, monkeypatch):
    monkeypatch.setattr(database.time, "sleep", lambda s: None)
    client = fake([{"novel_id": "n1", "users": {"telegram_user_id": 5}}])

    assert database.fetch_subscribers(["n1"]) == [{"novel_id": "n1", "users": {"telegram_user_id": 5}}]

    assert "users!inner(telegram_user_id)" in ops_of(client, 0, "select")[0][0][0]
    eq_args = [a for a, k in ops_of(client, 0, "eq")]
    assert ("reading_status", "reading") in eq_args
    assert ("users.status", "active") in eq_args


# ------------------------------------------------------------------ get_user / create_user

def test_get_user_returns_row_or_none(fake):
    fake([{"id": "u1", "username": "h", "telegram_user_id": 5, "status": "active"}], [])
    assert database.get_user(5)["id"] == "u1"
    assert database.get_user(6) is None


def test_get_user_lets_database_errors_propagate(fake):
    fake(RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        database.get_user(5)


def test_create_user_makes_auth_user_with_placeholder_email_then_users_row(fake):
    client = fake([{"id": "auth-1", "username": "hrithik", "telegram_user_id": 42, "status": "active"}])

    row = database.create_user(42, "hrithik")

    assert client.auth_calls == [("create_user", {"email": "tg42@scrollping.invalid", "email_confirm": True})]
    table, ops = sent(client, 0)
    assert table == "users"
    assert ops_of(client, 0, "insert")[0][0][0] == {"id": "auth-1", "username": "hrithik", "telegram_user_id": 42}
    assert row["id"] == "auth-1"


def test_create_user_deletes_the_auth_user_if_the_users_insert_fails(fake):
    client = fake(RuntimeError("insert failed"))

    with pytest.raises(RuntimeError):
        database.create_user(42, "h")

    assert ("delete_user", "auth-1") in client.auth_calls


def test_create_user_returns_existing_row_on_a_unique_violation(fake):
    existing = {"id": "u-old", "username": "h", "telegram_user_id": 42, "status": "active"}
    client = fake(UniqueViolation("duplicate key"), [existing])

    assert database.create_user(42, "h") == existing
    assert ("delete_user", "auth-1") in client.auth_calls


def test_create_user_still_raises_if_cleanup_also_fails(fake):
    client = fake(RuntimeError("insert failed"))
    client.delete_error = RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="insert failed"):
        database.create_user(42, "h")


def test_create_user_propagates_auth_failure_without_touching_users(fake):
    client = fake()
    client.create_error = RuntimeError("auth down")

    with pytest.raises(RuntimeError):
        database.create_user(42, "h")
    assert client.log == []


# ------------------------------------------------------------------ status / delete

@pytest.mark.parametrize("status", ["active", "paused"])
def test_set_user_status_updates_only_that_user(fake, status):
    client = fake([{"id": "u1"}])
    assert database.set_user_status("u1", status) is True
    assert ops_of(client, 0, "update")[0][0][0] == {"status": status}
    assert ops_of(client, 0, "eq")[0][0] == ("id", "u1")


def test_set_user_status_rejects_unknown_values_without_a_query(fake):
    client = fake()
    with pytest.raises(ValueError):
        database.set_user_status("u1", "banned")
    assert client.log == []


def test_set_user_status_false_when_no_row_matched(fake):
    fake([])
    assert database.set_user_status("missing", "paused") is False


def test_delete_user_account_deletes_the_auth_user(fake):
    client = fake()
    assert database.delete_user_account("u1") is True
    assert client.auth_calls == [("delete_user", "u1")]


# ------------------------------------------------------------------ websites

def test_get_or_create_website_returns_existing_id(fake):
    client = fake([{"id": "w1"}])
    assert database.get_or_create_website("https://hivetoons.org/") == "w1"
    assert len(client.log) == 1


def test_get_or_create_website_recreates_a_missing_row(fake):
    client = fake([], [{"id": "w9"}])
    assert database.get_or_create_website("https://hivetoons.org/") == "w9"
    assert ops_of(client, 1, "insert")[0][0][0] == {"web_domain_name": "https://hivetoons.org/"}


def test_get_or_create_website_survives_a_race(fake):
    fake([], UniqueViolation("duplicate key"), [{"id": "w2"}])
    assert database.get_or_create_website("https://hivetoons.org/") == "w2"


# ------------------------------------------------------------------ subscribe_user_to_novel

def test_subscribe_new_novel_creates_it_without_latest_chap(fake):
    client = fake(
        [],                      # 0 current subscriptions
        [{"id": "w1"}],          # website exists
        [],                      # novel does not exist
        [{"id": "n1"}],          # novel insert
        [{"id": "s1"}],          # subscription insert
    )

    assert database.subscribe_user_to_novel("u1", "https://hivetoons.org/", "some-novel", 20) == "added"

    novel_payload = ops_of(client, 3, "insert")[0][0][0]
    assert novel_payload["web_id"] == "w1"
    assert novel_payload["novel_name"] == "some-novel"
    assert "latest_chap" not in novel_payload          # None would bypass the -1 column default
    assert novel_payload["updated_at"]                 # lets the 97-day cron expire dead slugs
    assert ops_of(client, 4, "insert")[0][0][0] == {"user_id": "u1", "novel_id": "n1"}


def test_subscribe_existing_novel_does_not_create_it_again(fake):
    client = fake([], [{"id": "w1"}], [{"id": "n1"}], [], [{"id": "s1"}])

    assert database.subscribe_user_to_novel("u1", "k", "some-novel", 20) == "added"
    assert [table for table, ops in client.log] == [
        "user_subscriptions", "websites", "novels", "user_subscriptions", "user_subscriptions",
    ]


def test_subscribe_reports_already_subscribed(fake):
    client = fake([{"id": "s1"}], [{"id": "w1"}], [{"id": "n1"}], [{"id": "s1"}])

    assert database.subscribe_user_to_novel("u1", "k", "some-novel", 20) == "already_subscribed"
    assert len(client.log) == 4                         # nothing inserted


def test_subscribe_at_the_cap_refuses_without_creating_a_novel(fake):
    client = fake([{"id": f"s{i}"} for i in range(20)], [{"id": "w1"}], [])

    assert database.subscribe_user_to_novel("u1", "k", "new-novel", 20) == "limit_reached"
    assert [table for table, ops in client.log] == ["user_subscriptions", "websites", "novels"]


def test_subscribe_at_the_cap_still_says_already_subscribed_for_a_followed_novel(fake):
    fake([{"id": f"s{i}"} for i in range(20)], [{"id": "w1"}], [{"id": "n1"}], [{"id": "s1"}])
    assert database.subscribe_user_to_novel("u1", "k", "followed", 20) == "already_subscribed"


def test_subscribe_just_below_the_cap_is_allowed(fake):
    fake([{"id": f"s{i}"} for i in range(19)], [{"id": "w1"}], [{"id": "n1"}], [], [{"id": "s"}])
    assert database.subscribe_user_to_novel("u1", "k", "x", 20) == "added"


def test_subscribe_unique_violation_on_the_link_means_already_subscribed(fake):
    fake([], [{"id": "w1"}], [{"id": "n1"}], [], UniqueViolation("duplicate key"))
    assert database.subscribe_user_to_novel("u1", "k", "x", 20) == "already_subscribed"


def test_subscribe_novel_creation_race_reuses_the_winner(fake):
    client = fake([], [{"id": "w1"}], [], UniqueViolation("dup"), [{"id": "n7"}], [{"id": "s"}])
    assert database.subscribe_user_to_novel("u1", "k", "x", 20) == "added"
    assert ops_of(client, 5, "insert")[0][0][0] == {"user_id": "u1", "novel_id": "n7"}


def test_subscribe_propagates_real_database_errors(fake):
    fake([], [{"id": "w1"}], [], RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        database.subscribe_user_to_novel("u1", "k", "x", 20)


# ------------------------------------------------------------------ list / remove / status

def test_list_subscriptions_flattens_and_sorts_by_name(fake):
    fake([
        {"id": "s2", "reading_status": "reading",
         "novels": {"novel_name": "zeta", "latest_chap": 5, "websites": {"web_domain_name": "https://hivetoons.org/"}}},
        {"id": "s1", "reading_status": "left",
         "novels": {"novel_name": "alpha", "latest_chap": -1, "websites": {"web_domain_name": "https://vortexscans.org/"}}},
    ])

    assert database.list_subscriptions("u1") == [
        {"id": "s1", "reading_status": "left", "novel_name": "alpha", "latest_chap": -1,
         "web_domain_name": "https://vortexscans.org/"},
        {"id": "s2", "reading_status": "reading", "novel_name": "zeta", "latest_chap": 5,
         "web_domain_name": "https://hivetoons.org/"},
    ]


def test_list_subscriptions_tolerates_missing_nested_data(fake):
    fake([{"id": "s1", "reading_status": "reading", "novels": None}])
    row = database.list_subscriptions("u1")[0]
    assert row["novel_name"] == "?" and row["web_domain_name"] is None


def test_list_subscriptions_empty(fake):
    fake([])
    assert database.list_subscriptions("u1") == []


def test_remove_subscription_filters_on_subscription_and_owner(fake):
    client = fake([{"id": "s1"}])
    assert database.remove_subscription("u1", "s1") is True
    eq_args = [a for a, k in ops_of(client, 0, "eq")]
    assert ("id", "s1") in eq_args and ("user_id", "u1") in eq_args
    assert ops_of(client, 0, "delete")


def test_remove_subscription_false_when_nothing_matched(fake):
    fake([])
    assert database.remove_subscription("u1", "someone-elses") is False


@pytest.mark.parametrize("status", ["reading", "completed", "left", "on hiatus"])
def test_set_subscription_status_updates_with_ownership_filter(fake, status):
    client = fake([{"id": "s1"}])
    assert database.set_subscription_status("u1", "s1", status) is True
    assert ops_of(client, 0, "update")[0][0][0] == {"reading_status": status}
    eq_args = [a for a, k in ops_of(client, 0, "eq")]
    assert ("id", "s1") in eq_args and ("user_id", "u1") in eq_args


def test_set_subscription_status_rejects_unknown_values_without_a_query(fake):
    client = fake()
    with pytest.raises(ValueError):
        database.set_subscription_status("u1", "s1", "dropped")
    assert client.log == []


def test_set_subscription_status_false_when_nothing_matched(fake):
    fake([])
    assert database.set_subscription_status("u1", "nope", "left") is False
