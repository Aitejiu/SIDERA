"""Public native product composition; foundation data stays with its owners."""

from .app import create_app
from .composition import build_product_runtime
from .config import ProductSettings
from .lifecycle import ProductRuntime

__version__ = "0.1.0"
__all__ = ["ProductRuntime", "ProductSettings", "build_product_runtime", "create_app"]
