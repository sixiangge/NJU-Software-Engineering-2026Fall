"""An isolated simulated user walkthrough; no real user study is claimed."""

import json
import tempfile
import uuid
from pathlib import Path

from PIL import Image

from secondhand.application import Application
from secondhand.database import Database


def main():
    """Exercise seller, administrator and buyer without changing the user's database."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = Application(Database(root / "demo.db"))
        app.bootstrap("demo_admin", "DemoAdmin123")
        app.register("demo_seller", "DemoSeller123", "卖家")
        app.register("demo_buyer", "DemoBuyer123", "买家")
        admin = app.login("demo_admin", "DemoAdmin123")
        seller = app.login("demo_seller", "DemoSeller123")
        buyer = app.login("demo_buyer", "DemoBuyer123")
        category = app.category(admin, "数码", str(uuid.uuid4()))["id"]
        image = root / "keyboard.png"
        Image.new("RGB", (32, 32), "#236b83").save(image)
        draft = app.save_draft(
            seller,
            {
                "title": "机械键盘",
                "description": "功能正常，附数据线",
                "price": "99.50",
                "condition": "九成新",
                "location": "学校南门",
                "category_id": category,
            },
            str(uuid.uuid4()),
        )
        identifier = draft["id"]
        added = app.add_image(
            seller, identifier, draft["current_version"], image, str(uuid.uuid4())
        )
        submitted = app.item_action(
            seller, identifier, added["current_version"], "submit", str(uuid.uuid4())
        )
        approved = app.item_action(
            admin, identifier, submitted["current_version"], "approve", str(uuid.uuid4())
        )
        listing = app.browse(keyword="键盘", minimum="90", maximum="100")
        cancelled_claim = app.reserve(
            buyer, identifier, approved["current_version"], str(uuid.uuid4())
        )
        cancelled = app.reservation_action(
            buyer, cancelled_claim["id"], 1, "cancel", str(uuid.uuid4())
        )
        refreshed = app.item_detail(identifier)
        claim = app.reserve(buyer, identifier, refreshed["version"], str(uuid.uuid4()))
        confirmed = app.reservation_action(seller, claim["id"], 1, "confirm", str(uuid.uuid4()))
        key = str(uuid.uuid4())
        completed = app.reservation_action(buyer, claim["id"], 2, "complete", key)
        replay = app.reservation_action(buyer, claim["id"], 2, "complete", key)
        report = app.statistics(admin, "2026-01-01", "2030-12-31")
        output = {
            "notice": "模型编写脚本模拟操作；预约不涉及线上付款",
            "published": approved,
            "search_total": listing["total"],
            "cancelled": cancelled,
            "reserved": claim,
            "confirmed": confirmed,
            "completed": completed,
            "replay": replay,
            "statistics": report,
            "buyer_messages": app.messages(buyer),
            "invariant_errors": app.db.check(),
        }
        assert listing["total"] == 1
        assert report["sales"][0]["completed_count"] == 1
        assert replay["replayed"] and not output["invariant_errors"]
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
