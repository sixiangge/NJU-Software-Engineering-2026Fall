"""Domain values and validation; prices are stored as integer cents."""

import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class BusinessError(Exception):
    """A safe, user-facing business rejection."""


class ItemStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending_review"
    REJECTED = "rejected"
    PUBLISHED = "published"
    RESERVED = "reserved"
    TRADING = "trading"
    OFFLINE = "offline"
    VIOLATION = "violation"
    SOLD = "sold"


class ReservationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COMPLETED = "completed"
    CLOSED = "closed"


PUBLIC_STATES = ("published", "reserved", "trading", "sold")
ACTIVE_STATES = ("pending", "confirmed")
BEIJING = timezone(timedelta(hours=8))


def require(condition, message):
    """Reject a failed business precondition without exposing internals."""
    if not condition:
        raise BusinessError(message)


def text(value, name, maximum=200, required=True):
    """Normalize bounded human text and reject control characters."""
    require(isinstance(value, str), f"{name}必须是文本")
    value = value.strip()
    require(not required or bool(value), f"{name}不能为空")
    require(len(value) <= maximum, f"{name}不能超过{maximum}字符")
    require(not any(ord(char) < 32 and char not in "\n\t" for char in value), "存在控制字符")
    return value


def username(value):
    """Use one canonical username for registration and login."""
    value = text(value, "用户名", 40).casefold()
    require(bool(re.fullmatch(r"[\w.-]{3,40}", value)), "用户名须为3～40位字母、数字或_.-")
    return value


def password(value):
    """Validate the password without including it in any error."""
    require(isinstance(value, str), "密码必须是文本")
    require(8 <= len(value) <= 64, "密码须为8～64位")
    require(
        any(c.isalpha() for c in value) and any(c.isdigit() for c in value),
        "密码须同时包含字母和数字",
    )
    return value


def cents(value, allow_zero=False):
    """Convert a decimal string without floating point rounding."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BusinessError("价格格式错误") from exc
    require(amount.is_finite(), "价格须为有限数值")
    require(amount >= 0 if allow_zero else amount > 0, "价格必须为正数")
    require(amount <= Decimal("999999999.99"), "价格超出范围")
    require(amount * 100 == (amount * 100).to_integral_value(), "价格最多两位小数")
    return int(amount * 100)


def money(value):
    """Render stored cents exactly."""
    return f"{value // 100}.{value % 100:02d}"


def date_range(start, end):
    """Inclusive Beijing dates become a half-open UTC timestamp range."""
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
        require(first <= last, "开始日期不能晚于结束日期")
        lower = datetime.combine(first, time.min, BEIJING)
        upper = datetime.combine(last + timedelta(days=1), time.min, BEIJING)
        return lower.timestamp(), upper.timestamp()
    except (ValueError, OverflowError) as exc:
        raise BusinessError("日期须为YYYY-MM-DD且范围合法") from exc


def page_offset(page):
    """Page size is fixed by the requirements."""
    require(isinstance(page, int) and page >= 1, "页码须为正整数")
    return (page - 1) * 20
