"""Command-line desktop client; use shell to keep credentials only in memory."""

import argparse
import getpass
import json
import os
import shlex
import sqlite3
import uuid
from pathlib import Path

from secondhand.application import Application
from secondhand.database import Database
from secondhand.domain import BusinessError
from secondhand.operations import MaintenanceWorker, backup


def parser():
    """Expose explicit commands instead of allowing arbitrary database writes."""
    root = argparse.ArgumentParser(description="二手交易系统：预约不涉及线上付款")
    root.add_argument("--db", default="data/secondhand.db", help="SQLite数据库位置")
    root.add_argument("--request-id", help="重试时必须复用原请求标识")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("shell", help="交互操作；登录凭据仅保留在内存")
    commands.add_parser("profile", help="当前账号")
    commands.add_parser("logout", help="注销会话")
    commands.add_parser("maintenance", help="执行一次到期释放和消息投递")
    commands.add_parser("check", help="检查数据库与业务不变量")
    commands.add_parser("backup", help="在线备份，保留全部历史备份")
    commands.add_parser("invite", help="管理员生成一次性邀请码")
    commands.add_parser("categories", help="查询分类")
    for action in ("bootstrap", "register", "login"):
        command = commands.add_parser(action, help=f"{action}账号；密码安全输入")
        command.add_argument("username")
        if action != "login":
            command.add_argument("--nickname", default="用户")
        if action == "register":
            command.add_argument("--invite", help="受控管理员注册邀请码")
    command = commands.add_parser("category", help="创建或维护分类（管理员）")
    command.add_argument("name")
    command.add_argument("--id", type=int)
    command.add_argument("--disabled", action="store_true")
    command = commands.add_parser("revoke-invite", help="撤销未使用邀请码")
    command.add_argument("id", type=int)
    command = commands.add_parser("ban", help="封禁普通账号并撤销存量会话")
    command.add_argument("id", type=int)
    command.add_argument("reason")
    command = commands.add_parser("contact", help="维护本人可选联系方式")
    command.add_argument("value")
    command = commands.add_parser("draft", help="新建或保存不完整草稿")
    command.add_argument("--id", type=int)
    command.add_argument("--version", type=int)
    for field in ("title", "description", "price", "condition", "location"):
        command.add_argument(f"--{field}")
    command.add_argument("--category-id", type=int)
    command = commands.add_parser("image", help="给草稿添加实际JPEG/PNG图片")
    command.add_argument("id", type=int)
    command.add_argument("version", type=int)
    command.add_argument("path")
    command = commands.add_parser("remove-image", help="移除草稿图片")
    command.add_argument("id", type=int)
    command.add_argument("version", type=int)
    command.add_argument("image_id", type=int)
    command = commands.add_parser("export-image", help="导出有权访问的商品图片")
    command.add_argument("id", type=int)
    command.add_argument("image_id", type=int)
    command.add_argument("destination")
    command = commands.add_parser("item-action", help="按商品状态机执行命令")
    command.add_argument("id", type=int)
    command.add_argument("version", type=int)
    command.add_argument(
        "action",
        choices=[
            "submit",
            "withdraw",
            "edit",
            "take_down",
            "archive",
            "request_recovery",
            "approve",
            "reject",
            "allow_recovery",
        ],
    )
    command.add_argument("--reason", default="")
    command = commands.add_parser("violate", help="违规下架并同步关闭预约")
    command.add_argument("id", type=int)
    command.add_argument("version", type=int)
    command.add_argument("reason")
    command = commands.add_parser("browse", help="组合查询商品；每页20条")
    command.add_argument("--scope", choices=["public", "mine", "admin"], default="public")
    command.add_argument("--keyword", default="")
    command.add_argument("--category-id", type=int)
    command.add_argument("--minimum")
    command.add_argument("--maximum")
    command.add_argument("--status")
    command.add_argument("--sort", choices=["recent", "price_asc", "price_desc"], default="recent")
    command.add_argument("--page", type=int, default=1)
    command = commands.add_parser("item", help="查询详情、版本及图片编号")
    command.add_argument("id", type=int)
    command = commands.add_parser("reserve", help="预约不涉及线上付款")
    command.add_argument("id", type=int)
    command.add_argument("version", type=int)
    command = commands.add_parser("reservations", help="本人的买入、卖出预约；管理员可查全部")
    command.add_argument("--page", type=int, default=1)
    for action in ("reservation", "contacts"):
        command = commands.add_parser(action)
        command.add_argument("id", type=int)
    command = commands.add_parser("trade", help="交易动作；完成表示已收到线下交付的物品")
    command.add_argument("id", type=int)
    command.add_argument("version", type=int)
    command.add_argument(
        "action", choices=["cancel", "reject", "confirm", "complete", "release_confirm", "release"]
    )
    command.add_argument("--reason", default="")
    command = commands.add_parser("stats", help="记录成交金额，不代表真实线下付款")
    command.add_argument("start", help="北京时间YYYY-MM-DD")
    command.add_argument("end", help="包含结束日")
    command = commands.add_parser("messages", help="站内消息")
    command.add_argument("--unread", action="store_true")
    command.add_argument("--page", type=int, default=1)
    command = commands.add_parser("read-message")
    command.add_argument("id", type=int)
    command = commands.add_parser("audit", help="管理员组合查询审计日志")
    command.add_argument("--actor-id", type=int)
    command.add_argument("--operation")
    command.add_argument("--object-type", choices=["items", "reservations", "users", "request"])
    command.add_argument("--object-id", type=int)
    command.add_argument("--result", choices=["success", "failure"])
    command.add_argument("--start")
    command.add_argument("--end")
    command.add_argument("--page", type=int, default=1)
    command = commands.add_parser("result", help="用原请求标识查询已提交结果")
    command.add_argument("operation")
    command.add_argument("request")
    return root


class Client:
    """A local client session with no credentials written to disk."""

    def __init__(self, application):
        self.app = application
        self.credential = os.environ.get("SECONDHAND_TOKEN")
        self.last_command = None

    def execute(self, args):
        """Dispatch parsed input to the application boundary."""
        app, credential = self.app, self.credential
        request = args.request_id or str(uuid.uuid4())
        command = args.command
        if command in {"bootstrap", "register", "login"}:
            secret = getpass.getpass("密码（输入不回显）：")
            if command == "bootstrap":
                return {"id": app.bootstrap(args.username, secret, args.nickname)}
            if command == "register":
                return {"id": app.register(args.username, secret, args.nickname, args.invite)}
            self.credential = app.login(args.username, secret)
            return app.profile(self.credential)
        if command == "logout":
            if credential:
                app.logout(credential)
            self.credential = None
            return {"logged_out": True}
        if command == "profile":
            return app.profile(credential)
        if command == "browse":
            return app.browse(
                credential,
                args.scope,
                args.keyword,
                args.category_id,
                args.minimum,
                args.maximum,
                args.status,
                args.sort,
                args.page,
            )
        if command == "item":
            return app.item_detail(args.id, credential)
        if command == "categories":
            return app.categories(include_disabled=True)
        if command == "reservations":
            return app.reservations(credential, args.page)
        if command == "reservation":
            return app.reservation_detail(credential, args.id)
        if command == "contacts":
            return app.contacts(credential, args.id)
        if command == "messages":
            return app.messages(credential, args.unread, args.page)
        if command == "stats":
            return app.statistics(credential, args.start, args.end)
        if command == "audit":
            return app.audit_search(
                credential,
                args.actor_id,
                args.operation,
                args.object_type,
                args.object_id,
                args.result,
                args.start,
                args.end,
                args.page,
            )
        if command == "result":
            return app.request_result(credential, args.operation, args.request)
        if command == "export-image":
            return app.export_image(args.id, args.image_id, args.destination, credential)
        if command == "maintenance":
            return app.maintenance()
        if command == "check":
            errors = app.db.check()
            return {"ok": not errors, "errors": errors}
        if command == "backup":
            return {"path": backup(app.db, app.db.path.parent / "backups", app.clock)}
        if command == "invite":
            return app.create_invite(credential)
        if command == "read-message":
            app.read_message(credential, args.id)
            return {"read": args.id}
        # Print identifiers before execution so interruption cannot lose them.
        print(f"请求标识：{request}（重试请复用）")
        if command == "draft":
            fields = {
                key: getattr(args, key)
                for key in ("title", "description", "price", "condition", "location", "category_id")
                if getattr(args, key) is not None
            }
            return app.save_draft(credential, fields, request, args.id, args.version)
        if command == "image":
            return app.add_image(credential, args.id, args.version, args.path, request)
        if command == "remove-image":
            return app.remove_image(credential, args.id, args.version, args.image_id, request)
        if command == "category":
            return app.category(credential, args.name, request, args.id, not args.disabled)
        if command == "revoke-invite":
            return app.revoke_invite(credential, args.id, request)
        if command == "contact":
            return app.set_contact(credential, args.value, request)
        if command == "ban":
            return app.ban(credential, args.id, args.reason, request)
        if command == "item-action":
            return app.item_action(
                credential, args.id, args.version, args.action, request, args.reason
            )
        if command == "reserve":
            print("预约只表示购买意向，付款与交付在线下完成。")
            return app.reserve(credential, args.id, args.version, request)
        if command == "trade":
            return app.reservation_action(
                credential, args.id, args.version, args.action, request, args.reason
            )
        if command == "violate":
            return app.violate(credential, args.id, args.version, args.reason, request)
        raise BusinessError("未知命令")


def display(result):
    """JSON output is Unicode-friendly and can be captured as experiment evidence."""
    print(json.dumps(result, ensure_ascii=False, indent=2))


def shell(client, database_path):
    """Keep forms in command history and require explicit retries after failures."""
    command_parser = parser()
    worker = MaintenanceWorker(client.app, Path(database_path).resolve().parent / "backups")
    worker.start()
    print("二手交易系统 | help查看命令，exit退出；密码与登录令牌不会写入命令记录。")
    print("路径建议使用正斜杠；含空格的参数请加双引号。预约不涉及线上付款。")
    try:
        while True:
            try:
                if worker.error:
                    print("维护任务失败，将自动重试：", worker.error)
                line = input("secondhand> ").strip()
                if line in {"exit", "quit"}:
                    break
                if not line:
                    continue
                if line == "help":
                    command_parser.print_help()
                    continue
                arguments = command_parser.parse_args(shlex.split(line))
                if arguments.command == "shell":
                    print("当前已在交互模式中。")
                    continue
                arguments.request_id = arguments.request_id or str(uuid.uuid4())
                client.last_command = arguments
                display(client.execute(arguments))
            except SystemExit:
                continue
            except (BusinessError, OSError, sqlite3.Error) as exc:
                print(f"操作未成功：{exc}；请查看状态后使用原请求标识重试。")
    except (EOFError, KeyboardInterrupt):
        print("\n退出。")
    finally:
        worker.stop()
        if client.credential:
            client.app.logout(client.credential)


def main():
    """Installed console-script entry point."""
    arguments = parser().parse_args()
    try:
        client = Client(Application(Database(arguments.db)))
        if arguments.command == "shell":
            shell(client, arguments.db)
        else:
            display(client.execute(arguments))
    except (BusinessError, OSError, sqlite3.Error) as exc:
        print(f"操作未成功：{exc}")
        raise SystemExit(1) from exc
