import sys

from .config import ConfigError
from .main import main

try:
    main()
except ConfigError as e:
    sys.exit(f"❌ {e}")
