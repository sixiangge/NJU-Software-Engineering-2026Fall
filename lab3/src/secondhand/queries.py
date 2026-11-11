"""Permission-scoped reads, exact statistics, messages and audit searches."""

from secondhand.domain import PUBLIC_STATES, ItemStatus, cents, date_range, page_offset, require


class QueryService:
    """Projection methods never return password hashes or bearer credentials."""

    def browse(
        self,
        credential=None,
        scope="public",
        keyword="",
        category_id=None,
        minimum=None,
        maximum=None,
        status=None,
        sort="recent",
        page=1,
    ):
        """Combine conditions with AND and use stable twenty-row pages."""
        self.expire()
        require(scope in {"public", "mine", "admin"}, "查询范围错误")
        require(sort in {"recent", "price_asc", "price_desc"}, "排序方式错误")
        offset = page_offset(page)
        where, values = [], []
        with self.db.read() as connection:
            actor = self.actor(connection, credential) if credential else None
            if scope == "public":
                status = status or "published"
                require(status in PUBLIC_STATES, "公开查询不支持此状态")
                where.extend(["i.violation=0", "i.archived=0"])
            elif scope == "mine":
                require(actor is not None and actor["role"] == "user", "需要普通账号登录")
                where.append("i.seller_id=?")
                values.append(actor["id"])
            else:
                require(actor is not None and actor["role"] == "admin", "需要管理员权限")
            if status:
                require(status in set(ItemStatus), "商品状态错误")
                where.append("i.status=?")
                values.append(status)
            if keyword:
                require(isinstance(keyword, str) and len(keyword) <= 200, "关键词过长")
                # instr treats %, _ and backslashes as literal user text.
                where.append(
                    "(instr(lower(i.title),lower(?))>0 OR instr(lower(i.description),lower(?))>0)"
                )
                values.extend([keyword, keyword])
            if category_id is not None:
                where.append("i.category_id=?")
                values.append(category_id)
            low = cents(minimum, allow_zero=True) if minimum is not None else None
            high = cents(maximum, allow_zero=True) if maximum is not None else None
            require(low is None or high is None or low <= high, "价格下限不能大于上限")
            if low is not None:
                where.append("i.price_cents>=?")
                values.append(low)
            if high is not None:
                where.append("i.price_cents<=?")
                values.append(high)
            clause = " AND ".join(where) or "1=1"
            ordering = {
                "recent": "i.published_at DESC,i.id DESC",
                "price_asc": "i.price_cents ASC,i.id ASC",
                "price_desc": "i.price_cents DESC,i.id DESC",
            }[sort]
            total = connection.execute(
                f"SELECT COUNT(*) FROM items i WHERE {clause}", values
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT i.*,c.name category_name FROM items i "
                f"LEFT JOIN categories c ON c.id=i.category_id WHERE {clause} "
                f"ORDER BY {ordering} LIMIT 20 OFFSET ?",
                (*values, offset),
            )
            return {
                "total": total,
                "page": page,
                "page_size": 20,
                "items": [dict(row) for row in rows],
            }

    def item_detail(self, item_id, credential=None):
        """Hidden items remain visible only to their owner or an administrator."""
        self.expire()
        with self.db.read() as connection:
            actor = self.actor(connection, credential) if credential else None
            item = self.get(connection, "items", item_id)
            privileged = actor and (actor["role"] == "admin" or actor["id"] == item["seller_id"])
            require(
                privileged
                or (
                    item["status"] in PUBLIC_STATES
                    and not item["violation"]
                    and not item["archived"]
                ),
                "商品不存在或无权查看",
            )
            result = dict(item)
            result["images"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,mime_type,sort_no FROM images WHERE item_id=? ORDER BY sort_no",
                    (item_id,),
                )
            ]
            if privileged:
                result["reviews"] = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM reviews WHERE item_id=? ORDER BY id", (item_id,)
                    )
                ]
            return result

    def export_image(self, item_id, image_id, destination, credential=None):
        """Apply item visibility rules before writing image data locally."""
        from pathlib import Path

        self.item_detail(item_id, credential)
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT content FROM images WHERE item_id=? AND id=?", (item_id, image_id)
            ).fetchone()
            require(row is not None, "图片不存在")
            path = Path(destination)
            require(not path.exists(), "目标文件已存在，请选择新文件名")
            with path.open("xb") as output:
                output.write(row["content"])
        return str(path.resolve())

    def reservations(self, credential, page=1):
        """Buyers and sellers see their own history; admins see all."""
        self.expire()
        with self.db.read() as connection:
            actor = self.actor(connection, credential)
            where, values = "1=1", []
            if actor["role"] != "admin":
                where = "(r.buyer_id=? OR i.seller_id=?)"
                values.extend([actor["id"], actor["id"]])
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT r.*,i.seller_id FROM reservations r JOIN items i ON i.id=r.item_id "
                    f"WHERE {where} ORDER BY r.id DESC LIMIT 20 OFFSET ?",
                    (*values, page_offset(page)),
                )
            ]

    def reservation_detail(self, credential, reservation_id):
        """Snapshot history is available after a reservation reaches a terminal state."""
        self.expire()
        with self.db.read() as connection:
            actor = self.actor(connection, credential)
            reservation, item = self.participant(connection, actor, reservation_id)
            result = dict(reservation)
            result["item_status"] = item["status"]
            result["item_version"] = item["version"]
            result["seller_id"] = item["seller_id"]
            result["release_confirmations"] = [
                row[0]
                for row in connection.execute(
                    "SELECT user_id FROM release_confirmations WHERE reservation_id=?",
                    (reservation_id,),
                )
            ]
            return result

    def contacts(self, credential, reservation_id):
        """Active reservation membership is required; admin access is audited."""
        self.expire()
        with self.db.transaction() as connection:
            actor = self.actor(connection, credential)
            reservation, item = self.participant(connection, actor, reservation_id)
            require(
                reservation["status"] in {"pending", "confirmed"}, "预约已结束，联系方式权限已撤销"
            )
            rows = connection.execute(
                "SELECT id,nickname,contact FROM users WHERE id IN (?,?)",
                (reservation["buyer_id"], item["seller_id"]),
            )
            result = [dict(row) for row in rows]
            if actor["role"] == "admin":
                self.audit(connection, actor, "privacy_query", "reservations", reservation_id)
            return result

    def statistics(self, credential, start, end):
        """Keep inventory current while filtering completed trades by Beijing dates."""
        lower, upper = date_range(start, end)
        self.expire()
        with self.db.read() as connection:
            actor = self.actor(connection, credential)
            suffix, parameters = "", []
            if actor["role"] != "admin":
                suffix = " AND i.seller_id=?"
                parameters.append(actor["id"])
            inventory = [
                dict(row)
                for row in connection.execute(
                    "SELECT c.id,c.name,COUNT(i.id) published_count FROM categories c "
                    "LEFT JOIN items i ON i.category_id=c.id AND i.status='published' "
                    f"AND i.violation=0 {suffix} GROUP BY c.id ORDER BY c.id",
                    parameters,
                )
            ]
            sales = [
                dict(row)
                for row in connection.execute(
                    "SELECT r.category_snapshot category_id,r.category_name_snapshot category_name,"
                    "COUNT(*) completed_count,SUM(r.price_snapshot) recorded_amount_cents,"
                    "SUM(i.violation) flagged_count FROM reservations r "
                    "JOIN items i ON i.id=r.item_id "
                    "WHERE r.status='completed' AND r.completed_at>=? AND r.completed_at<? "
                    f"{suffix} GROUP BY r.category_snapshot,r.category_name_snapshot "
                    "ORDER BY category_id",
                    (lower, upper, *parameters),
                )
            ]
            return {
                "start": start,
                "end": end,
                "timezone": "UTC+08:00",
                "inventory": inventory,
                "sales": sales,
                "notice": "记录成交金额采用预约标价快照，不代表真实线下付款",
            }

    def messages(self, credential, unread=False, page=1):
        """Delivery is retried before displaying the private mailbox."""
        self.deliver()
        with self.db.read() as connection:
            actor = self.actor(connection, credential)
            clause = " AND read_at IS NULL" if unread else ""
            return [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM messages WHERE recipient_id=? {clause} "
                    "ORDER BY id DESC LIMIT 20 OFFSET ?",
                    (actor["id"], page_offset(page)),
                )
            ]

    def read_message(self, credential, message_id):
        """Reading another user's message cannot reveal or mutate it."""
        with self.db.transaction() as connection:
            actor = self.actor(connection, credential)
            cursor = connection.execute(
                "UPDATE messages SET read_at=COALESCE(read_at,?) WHERE id=? AND recipient_id=?",
                (self.clock(), message_id, actor["id"]),
            )
            require(cursor.rowcount == 1, "消息不存在或无权查看")

    def audit_search(
        self,
        credential,
        actor_id=None,
        operation=None,
        object_type=None,
        object_id=None,
        result=None,
        start=None,
        end=None,
        page=1,
    ):
        """All audit filter values are bound parameters, including identifiers."""
        where, parameters = [], []
        for key, value in (
            ("actor_id", actor_id),
            ("operation", operation),
            ("object_type", object_type),
            ("object_id", object_id),
            ("result", result),
        ):
            if value is not None:
                where.append(f"{key}=?")
                parameters.append(value)
        require((start is None) == (end is None), "日志日期须同时提供开始和结束")
        if start is not None:
            lower, upper = date_range(start, end)
            where.extend(["created_at>=?", "created_at<?"])
            parameters.extend([lower, upper])
        with self.db.read() as connection:
            self.actor(connection, credential, "admin")
            clause = " AND ".join(where) or "1=1"
            return [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM audit WHERE {clause} ORDER BY id DESC LIMIT 20 OFFSET ?",
                    (*parameters, page_offset(page)),
                )
            ]

    def request_result(self, credential, operation, request_id):
        """An absent row means unknown, rather than proof of business failure."""
        import json

        with self.db.read() as connection:
            actor = self.actor(connection, credential)
            row = connection.execute(
                "SELECT result FROM requests WHERE actor_id=? AND operation=? AND request_id=?",
                (actor["id"], operation, request_id),
            ).fetchone()
            if row is None:
                return {"known": False, "notice": "尚未查到已提交结果，请保留原标识重试"}
            result = json.loads(row["result"])
            if result.get("kind") == "reservations":
                current, _ = self.participant(connection, actor, result["id"])
                result["current_status"] = current["status"]
                result["current_version"] = current["version"]
            elif result.get("kind") == "items":
                current = self.get(connection, "items", result["id"])
                require(actor["role"] == "admin" or current["seller_id"] == actor["id"], "无权查看")
                result["current_status"] = current["status"]
                result["current_version"] = current["version"]
            return {"known": True, **result}
