"""Shared transaction, authorization, audit and durable request handling."""

import hashlib
import json
import sqlite3
import time

from secondhand.domain import BusinessError, require, text
from secondhand.security import digest


class Core:
    """Infrastructure shared by application services; no UI dependency."""

    def __init__(self, database, clock=time.time):
        self.db = database
        self.clock = clock

    def actor(self, connection, credential, role=None):
        """Revalidate the session and account on every call, including replay."""
        require(isinstance(credential, str) and bool(credential), "请先登录")
        user = connection.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (digest(credential), self.clock()),
        ).fetchone()
        require(user is not None, "登录已过期，请重新登录")
        require(not user["banned"], "账号已封禁")
        require(role is None or user["role"] == role, "当前角色无权执行此操作")
        return user

    @staticmethod
    def get(connection, table, identifier):
        """Table names come only from service constants, never UI input."""
        require(table in {"items", "reservations", "users", "categories", "invites"}, "非法对象")
        row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        require(row is not None, "记录不存在")
        return row

    @staticmethod
    def version(row, expected):
        """Reject stale screens, even if the state has cycled back."""
        require(
            isinstance(expected, int) and row["version"] == expected,
            f"版本冲突，请刷新；当前版本为{row['version']}",
        )

    def audit(
        self,
        connection,
        actor,
        operation,
        kind,
        identifier,
        request_id="",
        result="success",
        reason="",
        before="n/a",
        after="n/a",
    ):
        """Audit data intentionally excludes raw payloads and credentials."""
        connection.execute(
            "INSERT INTO audit(actor_id,role,operation,object_type,object_id,request_id,"
            "result,reason,before_state,after_state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                actor["id"] if actor else None,
                actor["role"] if actor else "system",
                operation,
                kind,
                identifier,
                request_id,
                result,
                reason,
                before,
                after,
                self.clock(),
            ),
        )

    def mutate(self, credential, operation, request_id, payload, authorize, execute):
        """Authorize before replay; result, audit and writes commit together."""
        request_id = text(request_id, "请求标识", 100)
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        actor_id = None
        try:
            with self.db.transaction() as connection:
                actor = self.actor(connection, credential)
                actor_id = actor["id"]
                authorize(connection, actor)
                old = connection.execute(
                    "SELECT * FROM requests WHERE actor_id=? AND operation=? AND request_id=?",
                    (actor_id, operation, request_id),
                ).fetchone()
                if old:
                    require(old["fingerprint"] == fingerprint, "同一请求标识不能用于不同内容")
                    result = json.loads(old["result"])
                    result["replayed"] = True
                else:
                    result = execute(connection, actor)
                    result["replayed"] = False
                    connection.execute(
                        "INSERT INTO requests VALUES(?,?,?,?,?,?)",
                        (
                            actor_id,
                            operation,
                            request_id,
                            fingerprint,
                            json.dumps(result, ensure_ascii=False),
                            self.clock(),
                        ),
                    )
                kind = result.get("kind")
                if kind in {"items", "reservations"}:
                    current = self.get(connection, kind, result["id"])
                    result["current_status"] = current["status"]
                    result["current_version"] = current["version"]
                return result
        except (BusinessError, sqlite3.IntegrityError) as exc:
            # A second transaction retains failure evidence after business rollback.
            with self.db.transaction() as connection:
                actor = connection.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone()
                self.audit(
                    connection,
                    actor,
                    operation,
                    "request",
                    None,
                    request_id,
                    "failure",
                    str(exc) if isinstance(exc, BusinessError) else "数据约束冲突",
                )
            if isinstance(exc, sqlite3.IntegrityError):
                raise BusinessError("数据约束冲突，请刷新后重试") from exc
            raise

    def item_owner(self, connection, actor, item_id):
        """Management authorization includes ownership, independent of state."""
        item = self.get(connection, "items", item_id)
        require(actor["role"] == "user" and item["seller_id"] == actor["id"], "只能操作自己的商品")
        return item

    def participant(self, connection, actor, reservation_id):
        """An administrator can inspect; only participants can trade."""
        reservation = self.get(connection, "reservations", reservation_id)
        item = self.get(connection, "items", reservation["item_id"])
        require(
            actor["role"] == "admin" or actor["id"] in (reservation["buyer_id"], item["seller_id"]),
            "无权访问此预约",
        )
        return reservation, item

    @staticmethod
    def admin(connection, actor):
        """Authorization callback usable by all controlled management operations."""
        require(actor["role"] == "admin", "需要管理员权限")

    def change_item(self, connection, item, status):
        """Every state mutation invalidates previously read versions."""
        connection.execute(
            "UPDATE items SET status=?,version=version+1,updated_at=? WHERE id=?",
            (status, self.clock(), item["id"]),
        )

    @staticmethod
    def outcome(kind, identifier, status):
        """A small stable result remains meaningful after later state changes."""
        return {"kind": kind, "id": identifier, "original_status": status}

    def notify(self, connection, reservation, item, action):
        """Outbox uniqueness prevents duplicate delivery across restarts."""
        for recipient in {reservation["buyer_id"], item["seller_id"]}:
            event_key = f"reservation:{reservation['id']}:{action}:{recipient}"
            connection.execute(
                "INSERT OR IGNORE INTO outbox"
                "(event_key,recipient_id,reservation_id,body,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    event_key,
                    recipient,
                    reservation["id"],
                    f"预约#{reservation['id']}：{action}；商品：{reservation['title_snapshot']}",
                    self.clock(),
                ),
            )

    def deliver(self):
        """Retryable local delivery; failures leave the business committed."""
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT * FROM outbox WHERE delivered=0").fetchall()
            for row in rows:
                connection.execute(
                    "INSERT OR IGNORE INTO messages"
                    "(event_id,recipient_id,reservation_id,body,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        row["id"],
                        row["recipient_id"],
                        row["reservation_id"],
                        row["body"],
                        self.clock(),
                    ),
                )
                connection.execute("UPDATE outbox SET delivered=1 WHERE id=?", (row["id"],))
            return len(rows)
