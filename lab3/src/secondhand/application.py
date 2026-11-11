"""Public application facade for command-line and future graphical clients."""

from secondhand.accounts import AccountService
from secondhand.catalog import CatalogService
from secondhand.core import Core
from secondhand.queries import QueryService
from secondhand.trading import TradingService


class Application(AccountService, CatalogService, TradingService, QueryService, Core):
    """Compose stateless service groups around one database and injectable clock."""

    def maintenance(self):
        """Safe to call at startup and repeatedly while the application runs."""
        return {"expired": self.expire(), "delivered": self.deliver()}
