"""Accounts, controlled administrator invitations and expiring sessions."""

import sqlite3

from secondhand import security
from secondhand.domain import BusinessError, password, require, text, username


class AccountService:
    """Account methods composed with Core by the application facade."""

    def bootstrap(self, name, secret, nickname="管理员"):
        """Initialize exactly one administrator; no built-in default password."""
        name, secret = username(name), password(secret)
        with self.db.transaction() as connection:
            require(
                not connection.execute("SELECT 1 FROM users WHERE role='admin'").fetchone(),
                "首位管理员已初始化",
            )
            identifier = connection.execute(
                "INSERT INTO users(username,password_hash,nickname,role,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    name,
                    security.hash_password(secret),
                    text(nickname, "昵称", 60),
                    "admin",
                    self.clock(),
                ),
            ).lastrowid
            actor = self.get(connection, "users", identifier)
            self.audit(connection, actor, "bootstrap", "users", identifier)
            return identifier

    def register(self, name, secret, nickname, invite=None):
        """Role is derived exclusively from a valid invite in the same transaction."""
        name, secret = username(name), password(secret)
        nickname = text(nickname, "昵称", 60)
        try:
            with self.db.transaction() as connection:
                invitation = None
                if invite is not None:
                    invitation = connection.execute(
                        "SELECT * FROM invites WHERE code_hash=?", (security.digest(invite),)
                    ).fetchone()
                    require(
                        invitation is not None
                        and not invitation["revoked"]
                        and invitation["used_by"] is None
                        and invitation["expires_at"] > self.clock(),
                        "邀请码无效或已到期",
                    )
                identifier = connection.execute(
                    "INSERT INTO users(username,password_hash,nickname,role,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        name,
                        security.hash_password(secret),
                        nickname,
                        "admin" if invitation else "user",
                        self.clock(),
                    ),
                ).lastrowid
                if invitation:
                    connection.execute(
                        "UPDATE invites SET used_by=? WHERE id=?", (identifier, invitation["id"])
                    )
                actor = self.get(connection, "users", identifier)
                self.audit(connection, actor, "register", "users", identifier)
                return identifier
        except (BusinessError, sqlite3.IntegrityError) as exc:
            with self.db.transaction() as connection:
                self.audit(
                    connection,
                    None,
                    "register",
                    "users",
                    None,
                    result="failure",
                    reason="注册校验失败或用户名重复",
                )
            if isinstance(exc, sqlite3.IntegrityError):
                raise BusinessError("用户名已存在") from exc
            raise

    def login(self, name, secret):
        """Failed attempts commit their counters and audit before raising."""
        name = username(name)
        credential = None
        with self.db.transaction() as connection:
            actor = connection.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
            allowed = (
                actor is not None and not actor["banned"] and actor["locked_until"] <= self.clock()
            )
            if allowed and security.verify_password(secret, actor["password_hash"]):
                credential = security.token()
                connection.execute(
                    "INSERT INTO sessions VALUES(?,?,?)",
                    (security.digest(credential), actor["id"], self.clock() + 7200),
                )
                connection.execute(
                    "UPDATE users SET failed_logins=0,locked_until=0,last_failure_at=0 WHERE id=?",
                    (actor["id"],),
                )
                self.audit(connection, actor, "login", "users", actor["id"])
            else:
                if allowed:
                    failures = (
                        actor["failed_logins"] + 1
                        if self.clock() - actor["last_failure_at"] < 900
                        else 1
                    )
                    connection.execute(
                        "UPDATE users SET failed_logins=?,locked_until=?,last_failure_at=? "
                        "WHERE id=?",
                        (
                            failures,
                            self.clock() + 900 if failures >= 5 else 0,
                            actor["last_failure_at"] if failures > 1 else self.clock(),
                            actor["id"],
                        ),
                    )
                self.audit(
                    connection,
                    actor,
                    "login",
                    "users",
                    actor["id"] if actor else None,
                    result="failure",
                    reason="凭据错误或账号不可用",
                )
        require(credential is not None, "用户名或密码错误，或账号当前不可用")
        return credential

    def logout(self, credential):
        """Explicit revocation also works for an already expired session."""
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash=?", (security.digest(credential),)
            )

    def profile(self, credential):
        """Return only non-secret account fields."""
        with self.db.read() as connection:
            actor = self.actor(connection, credential)
            return {key: actor[key] for key in ("id", "username", "nickname", "role", "contact")}

    def set_contact(self, credential, contact, request_id):
        """Optional local contact text; never included in audit payloads."""
        contact = text(contact, "联系方式", 200, required=False)

        def execute(connection, actor):
            connection.execute("UPDATE users SET contact=? WHERE id=?", (contact, actor["id"]))
            self.audit(connection, actor, "contact", "users", actor["id"], request_id)
            return {"id": actor["id"], "updated": True}

        return self.mutate(
            credential,
            "contact",
            request_id,
            {"contact": contact},
            lambda connection, actor: None,
            execute,
        )

    def create_invite(self, credential):
        """Only the plaintext response reveals the generated single-use code."""
        with self.db.transaction() as connection:
            actor = self.actor(connection, credential, "admin")
            code = security.token()
            identifier = connection.execute(
                "INSERT INTO invites(code_hash,creator_id,created_at,expires_at) VALUES(?,?,?,?)",
                (security.digest(code), actor["id"], self.clock(), self.clock() + 86400),
            ).lastrowid
            self.audit(connection, actor, "invite", "invites", identifier)
            return {"id": identifier, "code": code, "expires_at": self.clock() + 86400}

    def revoke_invite(self, credential, invite_id, request_id):
        """Used invitations remain in history and cannot be reused."""

        def execute(connection, actor):
            invite = self.get(connection, "invites", invite_id)
            require(invite["used_by"] is None, "邀请码已使用")
            connection.execute("UPDATE invites SET revoked=1 WHERE id=?", (invite_id,))
            self.audit(connection, actor, "revoke_invite", "invites", invite_id, request_id)
            return {"id": invite_id, "revoked": True}

        return self.mutate(
            credential, "revoke_invite", request_id, {"id": invite_id}, self.admin, execute
        )

    def ban(self, credential, user_id, reason, request_id):
        """Ban normal accounts; retain all business history and revoke sessions."""
        reason = text(reason, "封禁原因", 500)

        def execute(connection, actor):
            target = self.get(connection, "users", user_id)
            require(target["role"] == "user", "实验版本仅支持封禁普通账号")
            connection.execute("UPDATE users SET banned=1 WHERE id=?", (user_id,))
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.audit(connection, actor, "ban", "users", user_id, request_id, reason=reason)
            return {"id": user_id, "banned": True}

        return self.mutate(
            credential, "ban", request_id, {"id": user_id, "reason": reason}, self.admin, execute
        )
