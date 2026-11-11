"""Draft editing, validated images, moderation and catalog maintenance."""

import hashlib
import io
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from secondhand.domain import BusinessError, cents, require, text


class CatalogService:
    """All item changes go through ownership, version and state guards."""

    def category(self, credential, name, request_id, category_id=None, enabled=True):
        """Rename or disable in place so historical references survive."""
        name = text(name, "分类名称", 60)

        def execute(connection, actor):
            identifier = category_id
            if identifier is None:
                identifier = connection.execute(
                    "INSERT INTO categories(name,enabled) VALUES(?,?)", (name, int(enabled))
                ).lastrowid
            else:
                self.get(connection, "categories", identifier)
                connection.execute(
                    "UPDATE categories SET name=?,enabled=? WHERE id=?",
                    (name, int(enabled), identifier),
                )
            self.audit(connection, actor, "category", "categories", identifier, request_id)
            return {"id": identifier, "name": name, "enabled": bool(enabled)}

        return self.mutate(
            credential,
            "category",
            request_id,
            {"id": category_id, "name": name, "enabled": enabled},
            self.admin,
            execute,
        )

    def categories(self, include_disabled=False):
        """Category names are public; disabled rows are retained."""
        with self.db.read() as connection:
            clause = "" if include_disabled else "WHERE enabled=1"
            return [
                dict(row)
                for row in connection.execute(f"SELECT * FROM categories {clause} ORDER BY id")
            ]

    def save_draft(self, credential, fields, request_id, item_id=None, version=None):
        """Incomplete drafts are allowed; any supplied field must be valid."""
        allowed = {"title", "description", "price", "category_id", "condition", "location"}
        require(isinstance(fields, dict) and not fields.keys() - allowed, "未知商品字段")
        normalized = {}
        for key in ("title", "description", "condition", "location"):
            if key in fields:
                normalized[key] = text(
                    fields[key], key, 4000 if key == "description" else 200, required=False
                )
        if "price" in fields:
            normalized["price_cents"] = cents(fields["price"]) if fields["price"] else None
        if "category_id" in fields:
            normalized["category_id"] = fields["category_id"]

        def authorize(connection, actor):
            require(actor["role"] == "user", "管理员不参与买卖")
            if item_id is not None:
                self.item_owner(connection, actor, item_id)

        def execute(connection, actor):
            identifier = item_id
            if normalized.get("category_id") is not None:
                category = self.get(connection, "categories", normalized["category_id"])
                require(category["enabled"], "分类已停用")
            if identifier is None:
                identifier = connection.execute(
                    "INSERT INTO items(seller_id,created_at,updated_at) VALUES(?,?,?)",
                    (actor["id"], self.clock(), self.clock()),
                ).lastrowid
            else:
                item = self.item_owner(connection, actor, identifier)
                self.version(item, version)
                require(item["status"] == "draft" and not item["archived"], "请先返回可编辑草稿")
            if normalized:
                assignments = ",".join(f"{key}=?" for key in normalized)
                connection.execute(
                    f"UPDATE items SET {assignments},version=version+1,updated_at=? WHERE id=?",
                    (*normalized.values(), self.clock(), identifier),
                )
            self.audit(connection, actor, "save_draft", "items", identifier, request_id)
            return self.outcome("items", identifier, "draft")

        return self.mutate(
            credential,
            "save_draft",
            request_id,
            {"id": item_id, "version": version, "fields": fields},
            authorize,
            execute,
        )

    @staticmethod
    def validate_image(path):
        """Decode actual JPEG/PNG content, with compressed and pixel size limits."""
        path = Path(path)
        try:
            require(path.stat().st_size <= 5 * 1024 * 1024, "图片不能超过5MB")
            content = path.read_bytes()
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    require(image.format in {"JPEG", "PNG"}, "图片实际格式须为JPEG或PNG")
                    mime = "image/jpeg" if image.format == "JPEG" else "image/png"
                    require(image.width * image.height <= 40_000_000, "图片像素过大")
                    image.verify()
                with Image.open(io.BytesIO(content)) as image:
                    image.load()
            return content, mime
        except (
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise BusinessError("图片不存在、损坏或像素过大") from exc

    def add_image(self, credential, item_id, version, path, request_id):
        """Persist bytes with the item, so a later source-file move is harmless."""
        content, mime = self.validate_image(path)
        fingerprint = hashlib.sha256(content).hexdigest()

        def authorize(connection, actor):
            self.item_owner(connection, actor, item_id)

        def execute(connection, actor):
            item = self.item_owner(connection, actor, item_id)
            self.version(item, version)
            require(item["status"] == "draft" and not item["archived"], "只能给可编辑草稿添加图片")
            count = connection.execute(
                "SELECT COUNT(*) FROM images WHERE item_id=?", (item_id,)
            ).fetchone()[0]
            require(count < 9, "最多9张图片")
            sort_no = connection.execute(
                "SELECT COALESCE(MAX(sort_no),0)+1 FROM images WHERE item_id=?", (item_id,)
            ).fetchone()[0]
            image_id = connection.execute(
                "INSERT INTO images(item_id,content,mime_type,digest,sort_no) VALUES(?,?,?,?,?)",
                (item_id, content, mime, fingerprint, sort_no),
            ).lastrowid
            self.change_item(connection, item, "draft")
            self.audit(connection, actor, "add_image", "items", item_id, request_id)
            return {**self.outcome("items", item_id, "draft"), "image_id": image_id}

        return self.mutate(
            credential,
            "add_image",
            request_id,
            {"id": item_id, "version": version, "digest": fingerprint},
            authorize,
            execute,
        )

    def remove_image(self, credential, item_id, version, image_id, request_id):
        """Remove only draft image associations, never trade history."""

        def execute(connection, actor):
            item = self.item_owner(connection, actor, item_id)
            self.version(item, version)
            require(item["status"] == "draft" and not item["archived"], "只能编辑草稿图片")
            cursor = connection.execute(
                "DELETE FROM images WHERE id=? AND item_id=?", (image_id, item_id)
            )
            require(cursor.rowcount == 1, "图片不存在")
            self.change_item(connection, item, "draft")
            self.audit(connection, actor, "remove_image", "items", item_id, request_id)
            return self.outcome("items", item_id, "draft")

        return self.mutate(
            credential,
            "remove_image",
            request_id,
            {"id": item_id, "version": version, "image": image_id},
            lambda c, a: self.item_owner(c, a, item_id),
            execute,
        )

    def validate_submission(self, connection, item):
        """Approval never changes the content that was submitted for review."""
        for key in ("title", "description", "condition", "location", "price_cents", "category_id"):
            require(bool(item[key]), f"送审缺少字段：{key}")
        category = self.get(connection, "categories", item["category_id"])
        require(category["enabled"], "分类已停用，请重新选择")
        count = connection.execute(
            "SELECT COUNT(*) FROM images WHERE item_id=?", (item["id"],)
        ).fetchone()[0]
        require(1 <= count <= 9, "送审须有1～9张图片")

    def item_action(self, credential, item_id, version, action, request_id, reason=""):
        """Named commands encode the item state machine from the UML."""
        owner_actions = {"submit", "withdraw", "edit", "take_down", "archive", "request_recovery"}
        admin_actions = {"approve", "reject", "allow_recovery"}
        require(action in owner_actions | admin_actions, "未知商品操作")
        reason = text(reason, "原因", 500, required=action in {"reject", "request_recovery"})

        def authorize(connection, actor):
            if action in owner_actions:
                self.item_owner(connection, actor, item_id)
            else:
                self.admin(connection, actor)

        def execute(connection, actor):
            item = self.get(connection, "items", item_id)
            self.version(item, version)
            require(not item["archived"], "归档草稿不可操作")
            before = item["status"]
            target = before
            if action == "submit":
                require(before in {"draft", "offline"}, "当前状态不能送审")
                self.validate_submission(connection, item)
                target = "pending_review"
            elif action == "withdraw":
                require(before == "pending_review", "仅待审核可撤回")
                target = "draft"
            elif action == "edit":
                require(before in {"offline", "rejected"}, "须先下架或审核驳回才能返回草稿")
                target = "draft"
            elif action == "take_down":
                require(before == "published", "仅无预约的已发布商品可下架")
                target = "offline"
            elif action == "approve":
                require(before == "pending_review", "仅待审核商品可通过")
                self.validate_submission(connection, item)
                target = "published"
                connection.execute(
                    "UPDATE items SET published_at=? WHERE id=?", (self.clock(), item_id)
                )
            elif action == "reject":
                require(before == "pending_review", "仅待审核可驳回")
                target = "rejected"
            elif action == "archive":
                require(before == "draft", "只能归档草稿")
                connection.execute("UPDATE items SET archived=1 WHERE id=?", (item_id,))
            elif action == "request_recovery":
                require(before == "violation", "仅违规下架商品可申请整改")
                connection.execute("UPDATE items SET recovery_requested=1 WHERE id=?", (item_id,))
            elif action == "allow_recovery":
                require(before == "violation" and item["recovery_requested"], "须有整改申请")
                connection.execute(
                    "UPDATE items SET violation=0,recovery_requested=0 WHERE id=?", (item_id,)
                )
                target = "draft"
            self.change_item(connection, item, target)
            connection.execute("UPDATE items SET reason=? WHERE id=?", (reason, item_id))
            connection.execute(
                "INSERT INTO reviews(item_id,actor_id,reviewed_version,action,reason,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (item_id, actor["id"], version, action, reason, self.clock()),
            )
            self.audit(
                connection,
                actor,
                action,
                "items",
                item_id,
                request_id,
                reason=reason,
                before=before,
                after=target,
            )
            return self.outcome("items", item_id, target)

        return self.mutate(
            credential,
            action,
            request_id,
            {"id": item_id, "version": version, "reason": reason},
            authorize,
            execute,
        )
