"""Behavioral integration tests against isolated on-disk SQLite databases."""

import concurrent.futures
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from secondhand.application import Application
from secondhand.database import Database
from secondhand.domain import BusinessError, cents, date_range, password
from secondhand.operations import backup


class Scenario(unittest.TestCase):
    """Each scenario starts from independent accounts and a published item."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1794369600.0
        self.db = Database(self.root / "test.db")
        self.app = Application(self.db, lambda: self.now)
        self.app.bootstrap("admin", "Admin1234")
        self.seller_id = self.app.register("seller", "Seller1234", "卖家")
        self.buyer_id = self.app.register("buyer", "Buyer1234", "买家")
        self.other_id = self.app.register("other", "Other1234", "旁观者")
        self.admin = self.app.login("admin", "Admin1234")
        self.seller = self.app.login("seller", "Seller1234")
        self.buyer = self.app.login("buyer", "Buyer1234")
        self.other = self.app.login("other", "Other1234")
        self.category_id = self.app.category(self.admin, "数码", self.key())["id"]
        self.image = self.root / "photo.png"
        Image.new("RGB", (16, 16), "blue").save(self.image)
        self.item = self.publish()

    @staticmethod
    def key():
        return str(uuid.uuid4())

    def publish(self, title="二手键盘", price="99.50"):
        result = self.app.save_draft(
            self.seller,
            {
                "title": title,
                "description": "机械键盘，功能正常",
                "price": price,
                "category_id": self.category_id,
                "condition": "九成新",
                "location": "学校南门",
            },
            self.key(),
        )
        identifier = result["id"]
        version = result["current_version"]
        image = self.app.add_image(self.seller, identifier, version, self.image, self.key())
        review = self.app.item_action(
            self.seller, identifier, image["current_version"], "submit", self.key()
        )
        self.app.item_action(
            self.admin, identifier, review["current_version"], "approve", self.key()
        )
        return identifier

    def item_version(self, identifier=None):
        return self.app.item_detail(identifier or self.item, self.seller)["version"]

    def reserve(self, credential=None):
        return self.app.reserve(
            credential or self.buyer, self.item, self.item_version(), self.key()
        )

    def action(self, reservation, action, credential=None, reason=""):
        current = self.app.reservation_detail(credential or self.buyer, reservation)
        return self.app.reservation_action(
            credential or self.buyer, reservation, current["version"], action, self.key(), reason
        )

    def complete(self):
        reservation = self.reserve()["id"]
        self.action(reservation, "confirm", self.seller)
        self.action(reservation, "complete")
        return reservation

    def assert_consistent(self):
        self.assertEqual(self.db.check(), [])

    def test_full_trade_and_exact_statistics(self):
        self.complete()
        stats = self.app.statistics(self.admin, "2026-01-01", "2026-12-31")
        self.assertEqual(stats["sales"][0]["recorded_amount_cents"], 9950)
        self.assertEqual(stats["sales"][0]["completed_count"], 1)
        self.assertEqual(self.app.item_detail(self.item)["status"], "sold")
        self.assert_consistent()

    def test_normalized_username_unique(self):
        with self.assertRaises(BusinessError):
            self.app.register("  SELLER  ", "Password12", "同名")

    def test_password_boundaries(self):
        for value in ["abc1234", "a" * 64 + "1", "abcdefgh", "12345678"]:
            with self.subTest(value_length=len(value)), self.assertRaises(BusinessError):
                password(value)
        self.assertEqual(password("a1234567"), "a1234567")
        self.assertEqual(len(password("a" * 63 + "1")), 64)

    def test_prices_reject_invalid_and_nonfinite(self):
        for value in ["0", "-1", "NaN", "Infinity", "1.001", "invalid"]:
            with self.subTest(value=value), self.assertRaises(BusinessError):
                cents(value)
        self.assertEqual(cents("0.01"), 1)

    def test_self_and_admin_reservation_denied(self):
        for credential in (self.seller, self.admin):
            with self.assertRaises(BusinessError):
                self.reserve(credential)
        self.assert_consistent()

    def test_duplicate_request_replays_once(self):
        version, key = self.item_version(), self.key()
        results = [self.app.reserve(self.buyer, self.item, version, key) for _ in range(10)]
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertTrue(results[-1]["replayed"])
        self.assertEqual(len(self.app.audit_search(self.admin, operation="reserve")), 1)
        self.assert_consistent()

    def test_same_key_different_payload_denied(self):
        version, key = self.item_version(), self.key()
        self.app.reserve(self.buyer, self.item, version, key)
        with self.assertRaises(BusinessError):
            self.app.reserve(self.buyer, self.item, version + 1, key)

    def test_new_key_returns_existing_reservation(self):
        first = self.reserve()
        second = self.reserve()
        self.assertEqual(first["id"], second["id"])
        self.assert_consistent()

    def test_cancel_replay_cannot_release_later_claim(self):
        first = self.reserve()["id"]
        version, key = 1, self.key()
        self.app.reservation_action(self.buyer, first, version, "cancel", key)
        second = self.reserve(self.other)["id"]
        replay = self.app.reservation_action(self.buyer, first, version, "cancel", key)
        self.assertEqual(replay["current_status"], "cancelled")
        self.assertEqual(self.app.reservation_detail(self.other, second)["status"], "pending")
        self.assert_consistent()

    def test_stale_version_after_state_cycle_denied(self):
        old_version = self.item_version()
        reservation = self.reserve()["id"]
        self.action(reservation, "cancel")
        with self.assertRaises(BusinessError):
            self.app.reserve(self.other, self.item, old_version, self.key())

    def test_unauthorized_reservation_read_and_mutation(self):
        reservation = self.reserve()["id"]
        with self.assertRaises(BusinessError):
            self.app.reservation_detail(self.other, reservation)
        with self.assertRaises(BusinessError):
            self.app.reservation_action(self.other, reservation, 1, "cancel", self.key())

    def test_confirmed_cannot_cancel_or_timeout(self):
        reservation = self.reserve()["id"]
        self.action(reservation, "confirm", self.seller)
        self.now += 90000
        self.app.expire()
        # Sessions intentionally expire after two hours; obtain fresh credentials.
        self.buyer = self.app.login("buyer", "Buyer1234")
        with self.assertRaises(BusinessError):
            self.action(reservation, "cancel")
        self.assertEqual(
            self.app.reservation_detail(self.buyer, reservation)["status"], "confirmed"
        )
        self.assert_consistent()

    def test_expiry_boundary_persists_when_confirm_rejected(self):
        reservation = self.reserve()["id"]
        self.now += 86400
        self.seller = self.app.login("seller", "Seller1234")
        with self.assertRaises(BusinessError):
            self.app.reservation_action(self.seller, reservation, 1, "confirm", self.key())
        self.assertEqual(self.app.reservation_detail(self.seller, reservation)["status"], "expired")
        self.assert_consistent()

    def test_complete_replay_counts_one_sale(self):
        reservation = self.reserve()["id"]
        self.action(reservation, "confirm", self.seller)
        key = self.key()
        for _ in range(10):
            self.app.reservation_action(self.buyer, reservation, 2, "complete", key)
        stats = self.app.statistics(self.admin, "2026-01-01", "2026-12-31")
        self.assertEqual(stats["sales"][0]["completed_count"], 1)

    def test_release_requires_two_consents(self):
        reservation = self.reserve()["id"]
        self.action(reservation, "confirm", self.seller)
        with self.assertRaises(BusinessError):
            self.action(reservation, "release", self.admin, "双方协商")
        self.action(reservation, "release_confirm")
        with self.assertRaises(BusinessError):
            self.action(reservation, "release", self.admin, "双方协商")
        self.action(reservation, "release_confirm", self.seller)
        self.action(reservation, "release", self.admin, "双方协商")
        self.assertEqual(self.app.item_detail(self.item, self.seller)["status"], "offline")
        self.assert_consistent()

    def test_violation_closes_and_hides(self):
        reservation = self.reserve()["id"]
        self.app.violate(self.admin, self.item, self.item_version(), "违规描述", self.key())
        self.assertEqual(self.app.reservation_detail(self.buyer, reservation)["status"], "closed")
        with self.assertRaises(BusinessError):
            self.app.item_detail(self.item)
        self.assert_consistent()

    def test_sold_violation_retains_sale(self):
        self.complete()
        self.app.violate(self.admin, self.item, self.item_version(), "违规", self.key())
        stats = self.app.statistics(self.admin, "2026-01-01", "2026-12-31")
        self.assertEqual(stats["sales"][0]["flagged_count"], 1)
        self.assertEqual(stats["sales"][0]["recorded_amount_cents"], 9950)
        self.assert_consistent()

    def test_recovery_only_to_draft(self):
        self.app.violate(self.admin, self.item, self.item_version(), "违规", self.key())
        self.app.item_action(
            self.seller, self.item, self.item_version(), "request_recovery", self.key(), "已整改"
        )
        self.app.item_action(
            self.admin, self.item, self.item_version(), "allow_recovery", self.key()
        )
        self.assertEqual(self.app.item_detail(self.item, self.seller)["status"], "draft")
        self.assert_consistent()

    def test_contacts_revoked_after_cancel(self):
        self.app.set_contact(self.seller, "校内站点A", self.key())
        reservation = self.reserve()["id"]
        self.assertEqual(len(self.app.contacts(self.buyer, reservation)), 2)
        self.app.contacts(self.admin, reservation)
        self.assertEqual(len(self.app.audit_search(self.admin, operation="privacy_query")), 1)
        self.action(reservation, "cancel")
        with self.assertRaises(BusinessError):
            self.app.contacts(self.buyer, reservation)

    def test_ban_revokes_existing_session_and_replay(self):
        key, version = self.key(), self.item_version()
        self.app.reserve(self.buyer, self.item, version, key)
        self.app.ban(self.admin, self.buyer_id, "测试封禁", self.key())
        with self.assertRaises(BusinessError):
            self.app.reserve(self.buyer, self.item, version, key)
        with self.assertRaises(BusinessError):
            self.app.login("buyer", "Buyer1234")
        self.assert_consistent()

    def test_lockout_and_expiry(self):
        for _ in range(5):
            with self.assertRaises(BusinessError):
                self.app.login("buyer", "incorrect")
        with self.assertRaises(BusinessError):
            self.app.login("buyer", "Buyer1234")
        self.now += 901
        self.assertTrue(self.app.login("buyer", "Buyer1234"))

    def test_invite_consumed_atomically(self):
        invite = self.app.create_invite(self.admin)
        with self.assertRaises(BusinessError):
            self.app.register("seller", "Password12", "重名", invite["code"])
        self.app.register("newadmin", "Password12", "新管理员", invite["code"])
        with self.assertRaises(BusinessError):
            self.app.register("another", "Password12", "新管理员", invite["code"])

    def test_concurrent_buyers_one_winner(self):
        version = self.item_version()

        def attempt(credential):
            try:
                return self.app.reserve(credential, self.item, version, self.key())["id"]
            except BusinessError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [self.buyer, self.other]))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assert_consistent()

    def test_concurrent_cancel_and_confirm_one_winner(self):
        reservation = self.reserve()["id"]

        def attempt(arguments):
            credential, action = arguments
            try:
                self.app.reservation_action(credential, reservation, 1, action, self.key())
                return True
            except BusinessError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(attempt, [(self.buyer, "cancel"), (self.seller, "confirm")])
            )
        self.assertEqual(sum(results), 1)
        self.assert_consistent()

    def test_audit_failure_rolls_back_business(self):
        with (
            patch.object(self.app, "notify", side_effect=sqlite3.OperationalError("injected")),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.reserve()
        self.assertEqual(self.app.reservations(self.buyer), [])
        self.assertEqual(self.app.item_detail(self.item)["status"], "published")
        self.assert_consistent()

    def test_business_failure_retains_failure_log(self):
        with self.assertRaises(BusinessError):
            self.app.item_action(self.seller, self.item, self.item_version(), "submit", self.key())
        rows = self.app.audit_search(self.admin, operation="submit", result="failure")
        self.assertEqual(len(rows), 1)

    def test_outbox_retry_does_not_duplicate(self):
        self.reserve()
        self.app.deliver()
        self.app.deliver()
        self.assertEqual(len(self.app.messages(self.buyer)), 1)
        self.assertEqual(len(self.app.messages(self.seller)), 1)
        self.assertEqual(self.app.messages(self.other), [])

    def test_restart_replays_persisted_result(self):
        key, version = self.key(), self.item_version()
        first = self.app.reserve(self.buyer, self.item, version, key)
        restarted = Application(Database(self.db.path), lambda: self.now)
        second = restarted.reserve(self.buyer, self.item, version, key)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["replayed"])

    def test_backup_restore_is_consistent(self):
        self.reserve()
        path = backup(self.db, self.root / "backups", lambda: self.now)
        restored = Application(Database(path), lambda: self.now)
        self.assertEqual(len(restored.reservations(self.buyer)), 1)
        self.assertEqual(restored.db.check(), [])

    def test_incomplete_draft_and_invalid_filled_price(self):
        draft = self.app.save_draft(self.seller, {"title": "未完成"}, self.key())
        with self.assertRaises(BusinessError):
            self.app.item_action(
                self.seller, draft["id"], draft["current_version"], "submit", self.key()
            )
        with self.assertRaises(BusinessError):
            self.app.save_draft(self.seller, {"price": "-1"}, self.key())

    def test_fake_image_rejected(self):
        path = self.root / "fake.png"
        path.write_bytes(b"not an image")
        with self.assertRaises(BusinessError):
            self.app.validate_image(path)

    def test_disabled_category_blocks_submission(self):
        self.app.category(self.admin, "数码", self.key(), self.category_id, False)
        with self.assertRaises(BusinessError):
            self.app.save_draft(self.seller, {"category_id": self.category_id}, self.key())

    def test_withdraw_invalidates_review(self):
        self.app.item_action(self.seller, self.item, self.item_version(), "take_down", self.key())
        submitted = self.app.item_action(
            self.seller, self.item, self.item_version(), "submit", self.key()
        )
        self.app.item_action(
            self.seller, self.item, submitted["current_version"], "withdraw", self.key()
        )
        with self.assertRaises(BusinessError):
            self.app.item_action(
                self.admin, self.item, submitted["current_version"], "approve", self.key()
            )
        self.assert_consistent()

    def test_login_failure_window_resets(self):
        for _ in range(4):
            with self.assertRaises(BusinessError):
                self.app.login("buyer", "wrong")
        self.now += 901
        with self.assertRaises(BusinessError):
            self.app.login("buyer", "wrong")
        self.assertTrue(self.app.login("buyer", "Buyer1234"))

    def test_audit_insert_failure_rolls_back(self):
        with (
            patch.object(self.app, "audit", side_effect=sqlite3.OperationalError("injected")),
            self.assertRaises(sqlite3.OperationalError),
        ):
            self.reserve()
        self.assertEqual(self.app.reservations(self.buyer), [])
        self.assert_consistent()

    def test_complete_and_release_concurrent(self):
        reservation = self.reserve()["id"]
        self.action(reservation, "confirm", self.seller)
        self.action(reservation, "release_confirm")
        self.action(reservation, "release_confirm", self.seller)

        def attempt(arguments):
            credential, action = arguments
            try:
                self.app.reservation_action(
                    credential, reservation, 2, action, self.key(), "双方已确认"
                )
                return True
            except BusinessError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(attempt, [(self.buyer, "complete"), (self.admin, "release")])
            )
        self.assertEqual(sum(results), 1)
        self.assert_consistent()

    def test_query_intersection_and_literal_keyword(self):
        self.assertEqual(
            self.app.browse(keyword="键盘", minimum="99.50", maximum="99.50")["total"], 1
        )
        self.assertEqual(self.app.browse(keyword="%")["total"], 0)
        self.assertEqual(self.app.browse(keyword="' OR 1=1 --")["total"], 0)
        with self.assertRaises(BusinessError):
            self.app.browse(minimum="100", maximum="1")
        with self.assertRaises(BusinessError):
            self.app.browse(status="draft")

    def test_pagination_stable(self):
        for number in range(22):
            self.publish(f"商品{number}", "99.50")
        first = self.app.browse(sort="price_asc")["items"]
        second = self.app.browse(sort="price_asc", page=2)["items"]
        self.assertEqual((len(first), len(second)), (20, 3))
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in second})

    def test_historical_category_snapshot(self):
        self.complete()
        self.app.category(self.admin, "电子产品", self.key(), self.category_id)
        sales = self.app.statistics(self.admin, "2026-01-01", "2026-12-31")["sales"]
        self.assertEqual(sales[0]["category_name"], "数码")

    def test_beijing_date_boundaries(self):
        lower, upper = date_range("2026-11-11", "2026-11-11")
        self.assertEqual(upper - lower, 86400)
        self.assertEqual(lower, 1794326400)
        with self.assertRaises(BusinessError):
            date_range("2026-11-12", "2026-11-11")

    def test_unknown_result_is_not_failure(self):
        result = self.app.request_result(self.buyer, "reserve", "unknown")
        self.assertFalse(result["known"])

    def test_audit_and_statistics_scopes(self):
        self.complete()
        with self.assertRaises(BusinessError):
            self.app.audit_search(self.buyer)
        self.assertEqual(self.app.statistics(self.other, "2026-01-01", "2026-12-31")["sales"], [])


if __name__ == "__main__":
    unittest.main()
