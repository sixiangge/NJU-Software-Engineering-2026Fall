"""Transactional reservation state machine and administrator closure flows."""

from secondhand.domain import ACTIVE_STATES, require, text


class TradingService:
    """Commands preserve the item/reservation pair in one transaction."""

    def expire(self):
        """Release only pending reservations; confirmed trades never expire."""
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM reservations WHERE status='pending' AND expires_at<=?",
                (self.clock(),),
            ).fetchall()
            for reservation in rows:
                item = self.get(connection, "items", reservation["item_id"])
                self.end_reservation(connection, reservation, "expired", "预约超过24小时")
                self.change_item(connection, item, "published")
                self.audit(
                    connection,
                    None,
                    "expire",
                    "reservations",
                    reservation["id"],
                    f"expiry:{reservation['id']}",
                    before="pending",
                    after="expired",
                )
                self.notify(connection, reservation, item, "expired")
            return len(rows)

    def reserve(self, credential, item_id, version, request_id):
        """Snapshot title, cents and category at the unique successful claim."""
        self.expire()

        def authorize(connection, actor):
            require(actor["role"] == "user", "管理员不参与买卖")
            item = self.get(connection, "items", item_id)
            require(item["seller_id"] != actor["id"], "不能预约自己的商品")

        def execute(connection, actor):
            item = self.get(connection, "items", item_id)
            active = connection.execute(
                "SELECT * FROM reservations WHERE item_id=? AND status IN ('pending','confirmed')",
                (item_id,),
            ).fetchone()
            if active:
                require(active["buyer_id"] == actor["id"], "商品已被他人预约")
                return self.outcome("reservations", active["id"], active["status"])
            self.version(item, version)
            require(item["status"] == "published" and not item["violation"], "商品当前不可预约")
            seller = self.get(connection, "users", item["seller_id"])
            require(not seller["banned"], "卖家账号不可用，暂停新预约")
            category = self.get(connection, "categories", item["category_id"])
            identifier = connection.execute(
                "INSERT INTO reservations(item_id,buyer_id,status,created_at,expires_at,"
                "price_snapshot,title_snapshot,category_snapshot,category_name_snapshot) "
                "VALUES(?,?,'pending',?,?,?,?,?,?)",
                (
                    item_id,
                    actor["id"],
                    self.clock(),
                    self.clock() + 86400,
                    item["price_cents"],
                    item["title"],
                    category["id"],
                    category["name"],
                ),
            ).lastrowid
            self.change_item(connection, item, "reserved")
            reservation = self.get(connection, "reservations", identifier)
            self.audit(
                connection,
                actor,
                "reserve",
                "reservations",
                identifier,
                request_id,
                before="published",
                after="pending",
            )
            self.notify(connection, reservation, item, "pending")
            return self.outcome("reservations", identifier, "pending")

        return self.mutate(
            credential,
            "reserve",
            request_id,
            {"id": item_id, "version": version},
            authorize,
            execute,
        )

    def end_reservation(self, connection, reservation, status, reason):
        """Terminal records keep identifiers and immutable snapshots."""
        connection.execute(
            "UPDATE reservations SET status=?,reason=?,ended_at=?,version=version+1 WHERE id=?",
            (status, reason, self.clock(), reservation["id"]),
        )

    def reservation_action(
        self, credential, reservation_id, version, action, request_id, reason=""
    ):
        """Only the owning buyer/seller may perform their respective commands."""
        require(
            action in {"cancel", "reject", "confirm", "complete", "release_confirm", "release"},
            "未知预约操作",
        )
        reason = text(reason, "原因", 500, required=action in {"reject", "release"})
        self.expire()

        def authorize(connection, actor):
            reservation, item = self.participant(connection, actor, reservation_id)
            if action in {"cancel", "complete"}:
                require(actor["id"] == reservation["buyer_id"], "仅本笔买家可操作")
            elif action in {"reject", "confirm"}:
                require(actor["id"] == item["seller_id"], "仅所属卖家可操作")
            elif action == "release":
                self.admin(connection, actor)
            else:
                require(actor["role"] == "user", "解除确认须由交易双方本人提交")

        def execute(connection, actor):
            reservation, item = self.participant(connection, actor, reservation_id)
            self.version(reservation, version)
            before = reservation["status"]
            target = before
            if action in {"cancel", "reject", "confirm"}:
                require(
                    before == "pending" and self.clock() < reservation["expires_at"],
                    "预约已不处于有效待确认状态",
                )
                if action == "confirm":
                    target = "confirmed"
                    connection.execute(
                        "UPDATE reservations SET status='confirmed',confirmed_at=?,"
                        "version=version+1 "
                        "WHERE id=?",
                        (self.clock(), reservation_id),
                    )
                    self.change_item(connection, item, "trading")
                else:
                    target = "cancelled" if action == "cancel" else "rejected"
                    self.end_reservation(connection, reservation, target, reason)
                    self.change_item(connection, item, "published")
            elif action == "complete":
                require(before == "confirmed", "仅已确认预约可完成")
                target = "completed"
                self.end_reservation(connection, reservation, target, reason)
                connection.execute(
                    "UPDATE reservations SET completed_at=? WHERE id=?",
                    (self.clock(), reservation_id),
                )
                self.change_item(connection, item, "sold")
            elif action == "release_confirm":
                require(before == "confirmed", "仅交易中预约可协商解除")
                connection.execute(
                    "INSERT OR IGNORE INTO release_confirmations VALUES(?,?,?)",
                    (reservation_id, actor["id"], self.clock()),
                )
                # Independent consents do not invalidate the counterparty's screen.
            elif action == "release":
                require(before == "confirmed", "仅交易中预约可解除")
                consents = {
                    row[0]
                    for row in connection.execute(
                        "SELECT user_id FROM release_confirmations WHERE reservation_id=?",
                        (reservation_id,),
                    )
                }
                require(
                    consents == {reservation["buyer_id"], item["seller_id"]}, "须双方先确认解除"
                )
                target = "closed"
                self.end_reservation(connection, reservation, target, reason)
                self.change_item(connection, item, "offline")
            self.audit(
                connection,
                actor,
                action,
                "reservations",
                reservation_id,
                request_id,
                reason=reason,
                before=before,
                after=target,
            )
            if action != "release_confirm":
                self.notify(connection, reservation, item, target)
            return self.outcome("reservations", reservation_id, target)

        return self.mutate(
            credential,
            action,
            request_id,
            {"id": reservation_id, "version": version, "reason": reason},
            authorize,
            execute,
        )

    def violate(self, credential, item_id, version, reason, request_id):
        """Violation closes active claims, but never erases a completed sale."""
        reason = text(reason, "违规原因", 500)

        def execute(connection, actor):
            item = self.get(connection, "items", item_id)
            self.version(item, version)
            require(
                item["status"] in {"published", "reserved", "trading", "offline", "sold"},
                "当前商品状态不允许违规处理",
            )
            active = connection.execute(
                "SELECT * FROM reservations WHERE item_id=? AND status IN ('pending','confirmed')",
                (item_id,),
            ).fetchone()
            if active:
                require(active["status"] in ACTIVE_STATES, "预约状态冲突")
                self.end_reservation(connection, active, "closed", reason)
                self.audit(
                    connection,
                    actor,
                    "violation_close",
                    "reservations",
                    active["id"],
                    request_id,
                    reason=reason,
                    before=active["status"],
                    after="closed",
                )
                self.notify(connection, active, item, "violation_closed")
            target = "sold" if item["status"] == "sold" else "violation"
            self.change_item(connection, item, target)
            connection.execute(
                "UPDATE items SET violation=1,reason=? WHERE id=?", (reason, item_id)
            )
            connection.execute(
                "INSERT INTO reviews(item_id,actor_id,reviewed_version,action,reason,created_at) "
                "VALUES(?,?,?,'violate',?,?)",
                (item_id, actor["id"], version, reason, self.clock()),
            )
            self.audit(
                connection,
                actor,
                "violate",
                "items",
                item_id,
                request_id,
                reason=reason,
                before=item["status"],
                after=target,
            )
            return self.outcome("items", item_id, target)

        return self.mutate(
            credential,
            "violate",
            request_id,
            {"id": item_id, "version": version, "reason": reason},
            self.admin,
            execute,
        )
