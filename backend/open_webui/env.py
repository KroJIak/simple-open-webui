import datetime as dt
import importlib.metadata
import json
import logging
import os
import pkgutil
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import markdown
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import serialization

####################################
# Load .env file
####################################

# Use .resolve() to get the canonical path, removing any '..' or '.' components
ENV_FILE_PATH = Path(__file__).resolve()

# OPEN_WEBUI_DIR should be the directory where env.py resides (open_webui/)
OPEN_WEBUI_DIR = ENV_FILE_PATH.parent

# BACKEND_DIR is the parent of OPEN_WEBUI_DIR (backend/)
BACKEND_DIR = OPEN_WEBUI_DIR.parent

# BASE_DIR is the parent of BACKEND_DIR (open-webui-dev/)
BASE_DIR = BACKEND_DIR.parent

try:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(str(BASE_DIR / '.env')))
except ImportError:
    print('dotenv not installed, skipping...')

def get_env(name: str, default: str = '') -> str:
    """Read an environment variable, treating an empty value as "not set".

    A variable that is present but empty (a common result of a compose file
    referencing a variable that is missing from .env) must fall back to the
    default instead of being used as an empty string - int('') would crash
    the application on startup.
    """
    value = os.getenv(name)
    if value is None or value.strip() == '':
        return default
    return value


DOCKER = get_env('DOCKER', 'False').lower() == 'true'

# No local ML runtime in this deployment: embeddings and reranking are done
# through an OpenAI-compatible API, so torch is not installed at all.
DEVICE_TYPE = 'cpu'

####################################
# LOGGING
####################################

_LEVEL_MAP = {
    'DEBUG': 'debug',
    'INFO': 'info',
    'WARNING': 'warn',
    'ERROR': 'error',
    'CRITICAL': 'fatal',
}


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            'ts': dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(timespec='milliseconds'),
            'level': _LEVEL_MAP.get(record.levelname, record.levelname.lower()),
            'msg': record.getMessage(),
            'caller': record.name,
        }

        if record.exc_info and record.exc_info[0] is not None:
            log_entry['error'] = ''.join(traceback.format_exception(*record.exc_info)).rstrip()
        elif record.exc_text:
            log_entry['error'] = record.exc_text

        if record.stack_info:
            log_entry['stacktrace'] = record.stack_info

        return json.dumps(log_entry, ensure_ascii=False, default=str)


LOG_FORMAT = get_env('LOG_FORMAT', '').lower()

GLOBAL_LOG_LEVEL = get_env('GLOBAL_LOG_LEVEL', '').upper()
if GLOBAL_LOG_LEVEL in logging.getLevelNamesMapping():
    _log_cfg: dict[str, Any] = {'level': GLOBAL_LOG_LEVEL, 'force': True}
    if LOG_FORMAT == 'json':
        _json_handler = logging.StreamHandler(sys.stdout)
        _json_handler.setFormatter(JSONFormatter())
        _log_cfg['handlers'] = [_json_handler]
    else:
        _log_cfg['stream'] = sys.stdout
    logging.basicConfig(**_log_cfg)
else:
    GLOBAL_LOG_LEVEL = 'INFO'

log = logging.getLogger(__name__)
log.info('GLOBAL_LOG_LEVEL: %s', GLOBAL_LOG_LEVEL)

SRC_LOG_LEVELS = {}  # Legacy variable, do not remove

####################################
# ENV (dev,test,prod)
####################################

ENV = get_env('ENV', 'dev')

FROM_INIT_PY = get_env('FROM_INIT_PY', 'False').lower() == 'true'

if FROM_INIT_PY:
    PACKAGE_DATA = {'version': importlib.metadata.version('open-webui')}
else:
    try:
        PACKAGE_DATA = json.loads((BASE_DIR / 'package.json').read_text())
    except Exception:
        PACKAGE_DATA = {'version': '0.0.0'}

VERSION = PACKAGE_DATA['version']


DEPLOYMENT_ID = get_env('DEPLOYMENT_ID', '')
INSTANCE_ID = get_env('INSTANCE_ID', str(uuid4()))

ENABLE_DB_MIGRATIONS = get_env('ENABLE_DB_MIGRATIONS', 'True').lower() == 'true'


# Function to parse each section
def parse_section(section):
    items = []
    for li in section.find_all('li'):
        # Extract raw HTML string
        raw_html = str(li)

        # Extract text without HTML tags
        text = li.get_text(separator=' ', strip=True)

        # Split into title and content
        parts = text.split(': ', 1)
        title = parts[0].strip() if len(parts) > 1 else ''
        content = parts[1].strip() if len(parts) > 1 else text

        items.append({'title': title, 'content': content, 'raw': raw_html})
    return items


try:
    changelog_path = BASE_DIR / 'CHANGELOG.md'
    with open(str(changelog_path.absolute()), encoding='utf8') as file:
        changelog_content = file.read()

except Exception:
    changelog_content = (pkgutil.get_data('open_webui', 'CHANGELOG.md') or b'').decode()

# Convert markdown content to HTML
html_content = markdown.markdown(changelog_content)

# Parse the HTML content
soup = BeautifulSoup(html_content, 'html.parser')

# Initialize JSON structure
changelog_json = {}

# Iterate over each version
for version in soup.find_all('h2'):
    version_number = version.get_text().strip().split(' - ')[0][1:-1]  # Remove brackets
    date = version.get_text().strip().split(' - ')[1]

    version_data = {'date': date}

    # Find the next sibling that is a h3 tag (section title)
    current = version.find_next_sibling()

    while current and current.name != 'h2':
        if current.name == 'h3':
            section_title = current.get_text().lower()  # e.g., "added", "fixed"
            section_items = parse_section(current.find_next_sibling('ul'))
            version_data[section_title] = section_items

        # Move to the next element
        current = current.find_next_sibling()

    changelog_json[version_number] = version_data

CHANGELOG = changelog_json

####################################
# DATA/FRONTEND BUILD DIR
####################################

DATA_DIR = Path(get_env('DATA_DIR', BACKEND_DIR / 'data')).resolve()

if FROM_INIT_PY:
    NEW_DATA_DIR = Path(get_env('DATA_DIR', OPEN_WEBUI_DIR / 'data')).resolve()
    NEW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if the data directory exists in the package directory
    if DATA_DIR.exists() and DATA_DIR != NEW_DATA_DIR:
        log.info(f'Moving {DATA_DIR} to {NEW_DATA_DIR}')
        for item in DATA_DIR.iterdir():
            dest = NEW_DATA_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # Zip the data directory
        shutil.make_archive(DATA_DIR.parent / 'open_webui_data', 'zip', DATA_DIR)

        # Remove the old data directory
        shutil.rmtree(DATA_DIR)

    DATA_DIR = Path(get_env('DATA_DIR', OPEN_WEBUI_DIR / 'data'))

STATIC_DIR = Path(get_env('STATIC_DIR', OPEN_WEBUI_DIR / 'static'))

FONTS_DIR = Path(get_env('FONTS_DIR', OPEN_WEBUI_DIR / 'static' / 'fonts'))

FRONTEND_BUILD_DIR = Path(get_env('FRONTEND_BUILD_DIR', BASE_DIR / 'build')).resolve()

if FROM_INIT_PY:
    FRONTEND_BUILD_DIR = Path(get_env('FRONTEND_BUILD_DIR', OPEN_WEBUI_DIR / 'frontend')).resolve()

####################################
# Database
####################################

# Check if the file exists
if os.path.exists(f'{DATA_DIR}/ollama.db'):
    # Rename the file
    os.rename(f'{DATA_DIR}/ollama.db', f'{DATA_DIR}/webui.db')
    log.info('Database migrated from Ollama-WebUI successfully.')
else:
    pass

DATABASE_URL = get_env('DATABASE_URL', f'sqlite:///{DATA_DIR}/webui.db')

DATABASE_TYPE = get_env('DATABASE_TYPE')
DATABASE_USER = get_env('DATABASE_USER')
DATABASE_PASSWORD = get_env('DATABASE_PASSWORD')

DATABASE_CRED = ''
if DATABASE_USER:
    DATABASE_CRED += f'{DATABASE_USER}'
if DATABASE_PASSWORD:
    DATABASE_CRED += f':{DATABASE_PASSWORD}'

DB_VARS = {
    'db_type': DATABASE_TYPE,
    'db_cred': DATABASE_CRED,
    'db_host': get_env('DATABASE_HOST'),
    'db_port': get_env('DATABASE_PORT'),
    'db_name': get_env('DATABASE_NAME'),
}

if all(DB_VARS.values()):
    DATABASE_URL = (
        f'{DB_VARS["db_type"]}://{DB_VARS["db_cred"]}@{DB_VARS["db_host"]}:{DB_VARS["db_port"]}/{DB_VARS["db_name"]}'
    )
elif DATABASE_TYPE == 'sqlite+sqlcipher' and not get_env('DATABASE_URL'):
    # Handle SQLCipher with local file when DATABASE_URL wasn't explicitly set
    DATABASE_URL = f'sqlite+sqlcipher:///{DATA_DIR}/webui.db'

# Replace the postgres:// with postgresql://
if 'postgres://' in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://')

DATABASE_SCHEMA = get_env('DATABASE_SCHEMA', None)

_pool_size_raw = get_env('DATABASE_POOL_SIZE')
try:
    DATABASE_POOL_SIZE = int(_pool_size_raw) if _pool_size_raw else None
except (ValueError, TypeError):
    DATABASE_POOL_SIZE = None

_pool_overflow_raw = get_env('DATABASE_POOL_MAX_OVERFLOW', '0')
try:
    DATABASE_POOL_MAX_OVERFLOW = int(_pool_overflow_raw) if _pool_overflow_raw else 0
except (ValueError, TypeError):
    DATABASE_POOL_MAX_OVERFLOW = 0

_pool_timeout_raw = get_env('DATABASE_POOL_TIMEOUT', '30')
try:
    DATABASE_POOL_TIMEOUT = int(_pool_timeout_raw) if _pool_timeout_raw else 30
except (ValueError, TypeError):
    DATABASE_POOL_TIMEOUT = 30

_pool_recycle_raw = get_env('DATABASE_POOL_RECYCLE', '3600')
try:
    DATABASE_POOL_RECYCLE = int(_pool_recycle_raw) if _pool_recycle_raw else 3600
except (ValueError, TypeError):
    DATABASE_POOL_RECYCLE = 3600

DATABASE_ENABLE_SQLITE_WAL = get_env('DATABASE_ENABLE_SQLITE_WAL', 'True').lower() == 'true'

# SQLite PRAGMA tuning — these defaults are optimised for WAL-mode web-server
# workloads.  Each can be overridden via its environment variable.
# Set any value to an empty string to skip that PRAGMA entirely.

# PRAGMA synchronous: NORMAL (1) is safe with WAL and avoids an fsync per
# transaction.  Valid values: OFF (0), NORMAL (1), FULL (2), EXTRA (3).
DATABASE_SQLITE_PRAGMA_SYNCHRONOUS = get_env('DATABASE_SQLITE_PRAGMA_SYNCHRONOUS', 'NORMAL')

# PRAGMA busy_timeout (ms): how long a connection waits for a write lock
# before raising SQLITE_BUSY.
DATABASE_SQLITE_PRAGMA_BUSY_TIMEOUT = get_env('DATABASE_SQLITE_PRAGMA_BUSY_TIMEOUT', '5000')

# PRAGMA cache_size: negative value = KiB.  -65536 ≈ 64 MB page cache.
DATABASE_SQLITE_PRAGMA_CACHE_SIZE = get_env('DATABASE_SQLITE_PRAGMA_CACHE_SIZE', '-65536')

# PRAGMA temp_store: MEMORY (2) keeps temp tables and indices in RAM.
# Valid values: DEFAULT (0), FILE (1), MEMORY (2).
DATABASE_SQLITE_PRAGMA_TEMP_STORE = get_env('DATABASE_SQLITE_PRAGMA_TEMP_STORE', 'MEMORY')

# PRAGMA mmap_size (bytes): memory-mapped I/O size.  268435456 ≈ 256 MB.
# Set to 0 to disable mmap.
DATABASE_SQLITE_PRAGMA_MMAP_SIZE = get_env('DATABASE_SQLITE_PRAGMA_MMAP_SIZE', '268435456')

# PRAGMA journal_size_limit (bytes): caps the WAL file size after checkpoint.
# Without this the WAL grows unbounded during write bursts and is never
# truncated.  67108864 ≈ 64 MB.  Set to -1 for no limit (SQLite default).
DATABASE_SQLITE_PRAGMA_JOURNAL_SIZE_LIMIT = get_env('DATABASE_SQLITE_PRAGMA_JOURNAL_SIZE_LIMIT', '67108864')

DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL = get_env('DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL', None)
if DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL is not None:
    try:
        DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL = float(DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL)
    except Exception:
        DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL = 0.0

DATABASE_ENABLE_SESSION_SHARING = get_env('DATABASE_ENABLE_SESSION_SHARING', 'False').lower() == 'true'
ENABLE_PUBLIC_ACTIVE_USERS_COUNT = get_env('ENABLE_PUBLIC_ACTIVE_USERS_COUNT', 'True').lower() == 'true'
RESET_CONFIG_ON_START = get_env('RESET_CONFIG_ON_START', 'False').lower() == 'true'
ENABLE_REALTIME_CHAT_SAVE = get_env('ENABLE_REALTIME_CHAT_SAVE', 'False').lower() == 'true'
ENABLE_QUERIES_CACHE = get_env('ENABLE_QUERIES_CACHE', 'False').lower() == 'true'
RAG_SYSTEM_CONTEXT = get_env('RAG_SYSTEM_CONTEXT', 'False').lower() == 'true'

####################################
# REDIS
####################################

REDIS_URL = get_env('REDIS_URL', '')
REDIS_CLUSTER = get_env('REDIS_CLUSTER', 'False').lower() == 'true'

REDIS_KEY_PREFIX = get_env('REDIS_KEY_PREFIX', 'open-webui')

REDIS_SENTINEL_HOSTS = get_env('REDIS_SENTINEL_HOSTS', '')
REDIS_SENTINEL_PORT = get_env('REDIS_SENTINEL_PORT', '26379')

# Maximum number of retries for Redis operations when using Sentinel fail-over
REDIS_SENTINEL_MAX_RETRY_COUNT = get_env('REDIS_SENTINEL_MAX_RETRY_COUNT', '2')
try:
    REDIS_SENTINEL_MAX_RETRY_COUNT = int(REDIS_SENTINEL_MAX_RETRY_COUNT)
    if REDIS_SENTINEL_MAX_RETRY_COUNT < 1:
        REDIS_SENTINEL_MAX_RETRY_COUNT = 2
except ValueError:
    REDIS_SENTINEL_MAX_RETRY_COUNT = 2


REDIS_SOCKET_CONNECT_TIMEOUT = get_env('REDIS_SOCKET_CONNECT_TIMEOUT', '')
try:
    REDIS_SOCKET_CONNECT_TIMEOUT = float(REDIS_SOCKET_CONNECT_TIMEOUT)
except ValueError:
    REDIS_SOCKET_CONNECT_TIMEOUT = None

# Whether to enable TCP SO_KEEPALIVE on Redis client sockets. Opt-in:
# defaults to off so behavior is unchanged for existing deployments. When
# enabled, the kernel sends TCP keepalive probes on idle connections so
# half-closed sockets (e.g. after a silent firewall/LB reset or a NIC
# flap) are detected before the next command lands on them.
REDIS_SOCKET_KEEPALIVE = get_env('REDIS_SOCKET_KEEPALIVE', 'False').lower() == 'true'

# How often (in seconds) redis-py should PING an idle pooled connection
# before reusing it. Opt-in: defaults to unset (empty string) so behavior
# is unchanged for existing deployments. When set, should be shorter than
# the Redis server `timeout` setting and any firewall/LB idle timeout on
# the path to Redis, so stale connections are detected before a real
# command lands on them. Set to 0 or empty to disable.
REDIS_HEALTH_CHECK_INTERVAL = get_env('REDIS_HEALTH_CHECK_INTERVAL', '')
try:
    REDIS_HEALTH_CHECK_INTERVAL = int(REDIS_HEALTH_CHECK_INTERVAL)
    if REDIS_HEALTH_CHECK_INTERVAL <= 0:
        REDIS_HEALTH_CHECK_INTERVAL = None
except ValueError:
    REDIS_HEALTH_CHECK_INTERVAL = None

REDIS_RECONNECT_DELAY = get_env('REDIS_RECONNECT_DELAY', '')

if REDIS_RECONNECT_DELAY == '':
    REDIS_RECONNECT_DELAY = None
else:
    try:
        REDIS_RECONNECT_DELAY = float(REDIS_RECONNECT_DELAY)
        if REDIS_RECONNECT_DELAY < 0:
            REDIS_RECONNECT_DELAY = None
    except Exception:
        REDIS_RECONNECT_DELAY = None

####################################
# Uvicorn
####################################

try:
    UVICORN_WORKERS = max(int(get_env('UVICORN_WORKERS', '1')), 1)
except (ValueError, TypeError):
    UVICORN_WORKERS = 1

####################################
# WEBSOCKET SUPPORT
####################################

ENABLE_WEBSOCKET_SUPPORT = get_env('ENABLE_WEBSOCKET_SUPPORT', 'True').lower() == 'true'


WEBSOCKET_MANAGER = get_env('WEBSOCKET_MANAGER', '')

WEBSOCKET_REDIS_OPTIONS = get_env('WEBSOCKET_REDIS_OPTIONS', '')


if WEBSOCKET_REDIS_OPTIONS == '':
    if REDIS_SOCKET_CONNECT_TIMEOUT:
        WEBSOCKET_REDIS_OPTIONS = {'socket_connect_timeout': REDIS_SOCKET_CONNECT_TIMEOUT}
    else:
        log.debug('No WEBSOCKET_REDIS_OPTIONS provided, defaulting to None')
        WEBSOCKET_REDIS_OPTIONS = None
else:
    try:
        WEBSOCKET_REDIS_OPTIONS = json.loads(WEBSOCKET_REDIS_OPTIONS)
    except Exception:
        log.warning('Invalid WEBSOCKET_REDIS_OPTIONS, defaulting to None')
        WEBSOCKET_REDIS_OPTIONS = None

WEBSOCKET_REDIS_URL = get_env('WEBSOCKET_REDIS_URL', REDIS_URL)
WEBSOCKET_REDIS_CLUSTER = get_env('WEBSOCKET_REDIS_CLUSTER', str(REDIS_CLUSTER)).lower() == 'true'

websocket_redis_lock_timeout = get_env('WEBSOCKET_REDIS_LOCK_TIMEOUT', '60')

try:
    WEBSOCKET_REDIS_LOCK_TIMEOUT = int(websocket_redis_lock_timeout)
except ValueError:
    WEBSOCKET_REDIS_LOCK_TIMEOUT = 60

WEBSOCKET_SENTINEL_HOSTS = get_env('WEBSOCKET_SENTINEL_HOSTS', '')
WEBSOCKET_SENTINEL_PORT = get_env('WEBSOCKET_SENTINEL_PORT', '26379')
WEBSOCKET_SERVER_LOGGING = get_env('WEBSOCKET_SERVER_LOGGING', 'False').lower() == 'true'
WEBSOCKET_SERVER_ENGINEIO_LOGGING = (
    get_env(
        'WEBSOCKET_SERVER_ENGINEIO_LOGGING',
        get_env('WEBSOCKET_SERVER_LOGGING', 'False'),
    ).lower()
    == 'true'
)
WEBSOCKET_SERVER_PING_TIMEOUT = get_env('WEBSOCKET_SERVER_PING_TIMEOUT', '20')
try:
    WEBSOCKET_SERVER_PING_TIMEOUT = int(WEBSOCKET_SERVER_PING_TIMEOUT)
except ValueError:
    WEBSOCKET_SERVER_PING_TIMEOUT = 20

WEBSOCKET_SERVER_PING_INTERVAL = get_env('WEBSOCKET_SERVER_PING_INTERVAL', '25')
try:
    WEBSOCKET_SERVER_PING_INTERVAL = int(WEBSOCKET_SERVER_PING_INTERVAL)
except ValueError:
    WEBSOCKET_SERVER_PING_INTERVAL = 25

WEBSOCKET_EVENT_CALLER_TIMEOUT = get_env('WEBSOCKET_EVENT_CALLER_TIMEOUT', '')

if WEBSOCKET_EVENT_CALLER_TIMEOUT == '':
    WEBSOCKET_EVENT_CALLER_TIMEOUT = None
else:
    try:
        WEBSOCKET_EVENT_CALLER_TIMEOUT = int(WEBSOCKET_EVENT_CALLER_TIMEOUT)
    except ValueError:
        WEBSOCKET_EVENT_CALLER_TIMEOUT = 300


REQUESTS_VERIFY = get_env('REQUESTS_VERIFY', 'True').lower() == 'true'

_aiohttp_timeout_raw = get_env('AIOHTTP_CLIENT_TIMEOUT', '')
try:
    AIOHTTP_CLIENT_TIMEOUT = int(_aiohttp_timeout_raw) if _aiohttp_timeout_raw else None
except (ValueError, TypeError):
    AIOHTTP_CLIENT_TIMEOUT = 300

_aiohttp_read_bufsize_raw = get_env('AIOHTTP_CLIENT_READ_BUFSIZE', '')
try:
    AIOHTTP_CLIENT_READ_BUFSIZE = int(_aiohttp_read_bufsize_raw) if _aiohttp_read_bufsize_raw else 8 * 1024 * 1024
except (ValueError, TypeError):
    AIOHTTP_CLIENT_READ_BUFSIZE = 8 * 1024 * 1024

_aiohttp_max_line_size_raw = get_env('AIOHTTP_CLIENT_MAX_LINE_SIZE', '')
try:
    AIOHTTP_CLIENT_MAX_LINE_SIZE = (
        int(_aiohttp_max_line_size_raw) if _aiohttp_max_line_size_raw else 16 * 1024 * 1024
    )
except (ValueError, TypeError):
    AIOHTTP_CLIENT_MAX_LINE_SIZE = 16 * 1024 * 1024

_aiohttp_max_field_size_raw = get_env('AIOHTTP_CLIENT_MAX_FIELD_SIZE', '')
try:
    AIOHTTP_CLIENT_MAX_FIELD_SIZE = (
        int(_aiohttp_max_field_size_raw) if _aiohttp_max_field_size_raw else 64 * 1024
    )
except (ValueError, TypeError):
    AIOHTTP_CLIENT_MAX_FIELD_SIZE = 64 * 1024


AIOHTTP_CLIENT_SESSION_SSL = get_env('AIOHTTP_CLIENT_SESSION_SSL', 'True').lower() == 'true'

# When False (default), outbound HTTP requests do not follow 3xx redirects.
AIOHTTP_CLIENT_ALLOW_REDIRECTS = get_env('AIOHTTP_CLIENT_ALLOW_REDIRECTS', 'False').lower() == 'true'

# Optional User-Agent override for outbound web-loader fetches.  When set,
# SafeWebBaseLoader sends this value instead of the default python-requests UA
# which is aggressively blocked by Cloudflare, Wikipedia, and similar services.
USER_AGENT = get_env('USER_AGENT', '')

_model_list_timeout_raw = get_env(
    'AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST',
    get_env('AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST', '10'),
)
try:
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST = int(_model_list_timeout_raw) if _model_list_timeout_raw else None
except (ValueError, TypeError):
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST = 10

_tool_data_timeout_raw = get_env('AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA', '10')
try:
    AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA = int(_tool_data_timeout_raw) if _tool_data_timeout_raw else None
except (ValueError, TypeError):
    AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA = 10


AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL = get_env('AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL', 'True').lower() == 'true'

AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER = get_env('AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER', '')

if AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER == '':
    AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER = AIOHTTP_CLIENT_TIMEOUT
else:
    try:
        AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER = int(AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER)
    except Exception:
        AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER = AIOHTTP_CLIENT_TIMEOUT

# Timeout (in seconds) for the MCP session.initialize() handshake.
# The handshake performs a list-tools round-trip and can take tens of
# seconds on cold-start servers or servers exposing many tools.
MCP_INITIALIZE_TIMEOUT = get_env('MCP_INITIALIZE_TIMEOUT', '10')
try:
    MCP_INITIALIZE_TIMEOUT = int(MCP_INITIALIZE_TIMEOUT)
except (ValueError, TypeError):
    MCP_INITIALIZE_TIMEOUT = 10


####################################
# AIOHTTP Connection Pool
####################################

AIOHTTP_POOL_CONNECTIONS = get_env('AIOHTTP_POOL_CONNECTIONS', '')
if AIOHTTP_POOL_CONNECTIONS == '':
    AIOHTTP_POOL_CONNECTIONS = None
else:
    try:
        AIOHTTP_POOL_CONNECTIONS = int(AIOHTTP_POOL_CONNECTIONS)
    except ValueError:
        AIOHTTP_POOL_CONNECTIONS = None

AIOHTTP_POOL_CONNECTIONS_PER_HOST = get_env('AIOHTTP_POOL_CONNECTIONS_PER_HOST', '')
if AIOHTTP_POOL_CONNECTIONS_PER_HOST == '':
    AIOHTTP_POOL_CONNECTIONS_PER_HOST = None
else:
    try:
        AIOHTTP_POOL_CONNECTIONS_PER_HOST = int(AIOHTTP_POOL_CONNECTIONS_PER_HOST)
    except ValueError:
        AIOHTTP_POOL_CONNECTIONS_PER_HOST = None

AIOHTTP_POOL_DNS_TTL = get_env('AIOHTTP_POOL_DNS_TTL', '300')
try:
    AIOHTTP_POOL_DNS_TTL = int(AIOHTTP_POOL_DNS_TTL)
    if AIOHTTP_POOL_DNS_TTL < 0:
        AIOHTTP_POOL_DNS_TTL = 300
except ValueError:
    AIOHTTP_POOL_DNS_TTL = 300

RAG_EMBEDDING_TIMEOUT = get_env('RAG_EMBEDDING_TIMEOUT', '')

if RAG_EMBEDDING_TIMEOUT == '':
    RAG_EMBEDDING_TIMEOUT = None
else:
    try:
        RAG_EMBEDDING_TIMEOUT = int(RAG_EMBEDDING_TIMEOUT)
    except Exception:
        RAG_EMBEDDING_TIMEOUT = None


####################################
# Auth
####################################

WEBUI_AUTH = get_env('WEBUI_AUTH', 'True').lower() == 'true'

ENABLE_INITIAL_ADMIN_SIGNUP = get_env('ENABLE_INITIAL_ADMIN_SIGNUP', 'False').lower() == 'true'
ENABLE_SIGNUP_PASSWORD_CONFIRMATION = get_env('ENABLE_SIGNUP_PASSWORD_CONFIRMATION', 'False').lower() == 'true'

####################################
# Secret key & cookies
####################################

# WEBUI_JWT_SECRET_KEY is deprecated; use WEBUI_SECRET_KEY instead.
# No hardcoded fallback by design: the supported start scripts set/auto-generate it; unset is rejected below.
WEBUI_SECRET_KEY = get_env(
    'WEBUI_SECRET_KEY',
    get_env('WEBUI_JWT_SECRET_KEY', ''),
)

WEBUI_SESSION_COOKIE_SAME_SITE = get_env('WEBUI_SESSION_COOKIE_SAME_SITE', 'lax')
WEBUI_SESSION_COOKIE_SECURE = get_env('WEBUI_SESSION_COOKIE_SECURE', 'false').lower() == 'true'
WEBUI_AUTH_COOKIE_SAME_SITE = get_env('WEBUI_AUTH_COOKIE_SAME_SITE', WEBUI_SESSION_COOKIE_SAME_SITE)
WEBUI_AUTH_COOKIE_SECURE = (
    get_env(
        'WEBUI_AUTH_COOKIE_SECURE',
        get_env('WEBUI_SESSION_COOKIE_SECURE', 'false'),
    ).lower()
    == 'true'
)

if WEBUI_AUTH and WEBUI_SECRET_KEY == '':
    raise SystemExit(
        'WEBUI_SECRET_KEY is not set. It is a hard requirement when authentication is enabled.\n'
        'The supported start methods set or auto-generate it for you: use start.sh (Linux/macOS), '
        'start_windows.bat (Windows), or `open-webui serve`.\n'
        'If you start the backend another way (e.g. invoking uvicorn directly, which is unsupported), '
        'you must set WEBUI_SECRET_KEY yourself to a long random value.\n'
        'See https://docs.openwebui.com/reference/env-configuration#webui_secret_key'
    )

ENABLE_COMPRESSION_MIDDLEWARE = get_env('ENABLE_COMPRESSION_MIDDLEWARE', 'True').lower() == 'true'

####################################
# Admin Account Runtime Creation
####################################

# Optional env vars for creating an admin account on startup
# Useful for headless/automated deployments
WEBUI_ADMIN_EMAIL = get_env('WEBUI_ADMIN_EMAIL', '')
WEBUI_ADMIN_PASSWORD = get_env('WEBUI_ADMIN_PASSWORD', '')
WEBUI_ADMIN_NAME = get_env('WEBUI_ADMIN_NAME', 'Admin')

WEBUI_AUTH_TRUSTED_EMAIL_HEADER = get_env('WEBUI_AUTH_TRUSTED_EMAIL_HEADER', None)
WEBUI_AUTH_TRUSTED_NAME_HEADER = get_env('WEBUI_AUTH_TRUSTED_NAME_HEADER', None)
WEBUI_AUTH_TRUSTED_GROUPS_HEADER = get_env('WEBUI_AUTH_TRUSTED_GROUPS_HEADER', None)
WEBUI_AUTH_TRUSTED_ROLE_HEADER = get_env('WEBUI_AUTH_TRUSTED_ROLE_HEADER', None)

# Custom header name for API key authentication.  Defaults to 'x-api-key'.
# Useful when Open WebUI sits behind a reverse proxy / API gateway that
# already uses the Authorization header for its own authentication — set
# this to a unique header (e.g. 'X-OpenWebUI-Key') so the middleware
# checks the custom header instead and avoids the 401 short-circuit.
CUSTOM_API_KEY_HEADER = get_env('CUSTOM_API_KEY_HEADER', 'x-api-key')

ENABLE_PASSWORD_VALIDATION = get_env('ENABLE_PASSWORD_VALIDATION', 'False').lower() == 'true'
PASSWORD_VALIDATION_REGEX_PATTERN = get_env(
    'PASSWORD_VALIDATION_REGEX_PATTERN',
    r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^\w\s]).{8,}$',
)


try:
    PASSWORD_VALIDATION_REGEX_PATTERN = rf'{PASSWORD_VALIDATION_REGEX_PATTERN}'
    PASSWORD_VALIDATION_REGEX_PATTERN = re.compile(PASSWORD_VALIDATION_REGEX_PATTERN)
except Exception as e:
    log.error(f'Invalid PASSWORD_VALIDATION_REGEX_PATTERN: {e}')
    PASSWORD_VALIDATION_REGEX_PATTERN = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^\w\s]).{8,}$')

PASSWORD_VALIDATION_HINT = get_env('PASSWORD_VALIDATION_HINT', '')


BYPASS_MODEL_ACCESS_CONTROL = get_env('BYPASS_MODEL_ACCESS_CONTROL', 'False').lower() == 'true'
BYPASS_RETRIEVAL_ACCESS_CONTROL = get_env('BYPASS_RETRIEVAL_ACCESS_CONTROL', 'False').lower() == 'true'

# When True, collection names that do not match any known file-*, user-memory-*,
# web-search-*, or knowledge-base collection are allowed through access control
# for non-admin users.  When False (default), unknown collection names are
# denied — closing the legacy unscoped namespace.
ENABLE_RETRIEVAL_UNSCOPED_COLLECTIONS = get_env('ENABLE_RETRIEVAL_UNSCOPED_COLLECTIONS', 'False').lower() == 'true'

# When enabled, skips pydub-based preprocessing (format conversion, compression,
# and chunked splitting) before sending files to processing engines. Useful when
# the upstream provider handles these steps or when ffmpeg is unavailable.
BYPASS_PYDUB_PREPROCESSING = get_env('BYPASS_PYDUB_PREPROCESSING', 'False').lower() == 'true'

# When disabled (default), the OpenAI catch-all proxy endpoint (/{path:path})
# is blocked. Enable only if you need direct passthrough to upstream OpenAI-
# compatible APIs for endpoints not natively handled by Open WebUI.
ENABLE_OPENAI_API_PASSTHROUGH = get_env('ENABLE_OPENAI_API_PASSTHROUGH', 'False').lower() == 'true'

WEBUI_AUTH_SIGNOUT_REDIRECT_URL = get_env('WEBUI_AUTH_SIGNOUT_REDIRECT_URL', None)

####################################
# OAUTH Configuration
####################################
ENABLE_OAUTH_EMAIL_FALLBACK = get_env('ENABLE_OAUTH_EMAIL_FALLBACK', 'False').lower() == 'true'

ENABLE_OAUTH_ID_TOKEN_COOKIE = get_env('ENABLE_OAUTH_ID_TOKEN_COOKIE', 'True').lower() == 'true'

OAUTH_CLIENT_INFO_ENCRYPTION_KEY = get_env('OAUTH_CLIENT_INFO_ENCRYPTION_KEY', WEBUI_SECRET_KEY)

OAUTH_SESSION_TOKEN_ENCRYPTION_KEY = get_env('OAUTH_SESSION_TOKEN_ENCRYPTION_KEY', WEBUI_SECRET_KEY)

# Maximum number of concurrent OAuth sessions per user per provider
# This prevents unbounded session growth while allowing multi-device usage
OAUTH_MAX_SESSIONS_PER_USER = int(get_env('OAUTH_MAX_SESSIONS_PER_USER', '10'))

# Token Exchange Configuration
# Allows external apps to exchange OAuth tokens for OpenWebUI tokens
ENABLE_OAUTH_TOKEN_EXCHANGE = get_env('ENABLE_OAUTH_TOKEN_EXCHANGE', 'False').lower() == 'true'

# Back-Channel Logout Configuration
# When enabled, exposes POST /oauth/backchannel-logout for IdP-initiated logout
# per OpenID Connect Back-Channel Logout 1.0 spec.
# Requires Redis for JWT revocation.
ENABLE_OAUTH_BACKCHANNEL_LOGOUT = get_env('ENABLE_OAUTH_BACKCHANNEL_LOGOUT', 'False').lower() == 'true'

####################################
# SCIM Configuration
####################################

ENABLE_SCIM = get_env('ENABLE_SCIM', get_env('SCIM_ENABLED', 'False')).lower() == 'true'
SCIM_TOKEN = get_env('SCIM_TOKEN', '')
SCIM_AUTH_PROVIDER = get_env('SCIM_AUTH_PROVIDER', '')

if ENABLE_SCIM and not SCIM_AUTH_PROVIDER:
    log.warning(
        'SCIM is enabled but SCIM_AUTH_PROVIDER is not set. '
        "Set SCIM_AUTH_PROVIDER to the OAuth provider name (e.g. 'microsoft', 'oidc') "
        'to enable externalId storage.'
    )

####################################
# LICENSE_KEY
####################################

LICENSE_KEY = get_env('LICENSE_KEY', '')

LICENSE_BLOB = None
LICENSE_BLOB_PATH = get_env('LICENSE_BLOB_PATH', DATA_DIR / 'l.data')
if LICENSE_BLOB_PATH and os.path.exists(LICENSE_BLOB_PATH):
    with open(LICENSE_BLOB_PATH, 'rb') as f:
        LICENSE_BLOB = f.read()

LICENSE_PUBLIC_KEY = get_env('LICENSE_PUBLIC_KEY', '')

pk = None
if LICENSE_PUBLIC_KEY:
    pk = serialization.load_pem_public_key(
        f"""
-----BEGIN PUBLIC KEY-----
{LICENSE_PUBLIC_KEY}
-----END PUBLIC KEY-----
""".encode()
    )


####################################
# WEBUI Identity
####################################

WEBUI_NAME = get_env('WEBUI_NAME', 'Open WebUI')

WEBUI_FAVICON_URL = get_env('WEBUI_FAVICON_URL', '/static/chatgpt-logo.svg')
WEBUI_BUILD_HASH = get_env('WEBUI_BUILD_HASH', 'dev-build')
TRUSTED_SIGNATURE_KEY = get_env('TRUSTED_SIGNATURE_KEY', '')

####################################
# Feature flags
####################################

SAFE_MODE = get_env('SAFE_MODE', 'False').lower() == 'true'
ENABLE_EASTER_EGGS = get_env('ENABLE_EASTER_EGGS', 'True').lower() == 'true'
ENABLE_STAR_SESSIONS_MIDDLEWARE = get_env('ENABLE_STAR_SESSIONS_MIDDLEWARE', 'False').lower() == 'true'
ENABLE_KB_EXEC = get_env('ENABLE_KB_EXEC', 'False').lower() == 'true'

ENABLE_PROFILE_IMAGE_URL_FORWARDING = get_env('ENABLE_PROFILE_IMAGE_URL_FORWARDING', 'True').lower() == 'true'
PROFILE_IMAGE_ALLOWED_MIME_TYPES = frozenset(
    t.strip()
    for t in get_env(
        'PROFILE_IMAGE_ALLOWED_MIME_TYPES',
        'image/png,image/jpeg,image/gif,image/webp',
    ).split(',')
    if t.strip()
)

# Max stored length (bytes) of a data:image profile URI; bounds Postgres/Redis
# bloat from inline avatars and model icons. Unset (default) disables the cap.
_profile_image_max_data_uri_size = get_env('PROFILE_IMAGE_MAX_DATA_URI_SIZE', '').strip()
PROFILE_IMAGE_MAX_DATA_URI_SIZE = int(_profile_image_max_data_uri_size) if _profile_image_max_data_uri_size else None

####################################
# Forward Headers
####################################

ENABLE_FORWARD_USER_INFO_HEADERS = get_env('ENABLE_FORWARD_USER_INFO_HEADERS', 'False').lower() == 'true'

FORWARD_USER_INFO_HEADER_USER_NAME = get_env('FORWARD_USER_INFO_HEADER_USER_NAME', 'X-OpenWebUI-User-Name')
FORWARD_USER_INFO_HEADER_USER_ID = get_env('FORWARD_USER_INFO_HEADER_USER_ID', 'X-OpenWebUI-User-Id')
FORWARD_USER_INFO_HEADER_USER_EMAIL = get_env('FORWARD_USER_INFO_HEADER_USER_EMAIL', 'X-OpenWebUI-User-Email')
FORWARD_USER_INFO_HEADER_USER_ROLE = get_env('FORWARD_USER_INFO_HEADER_USER_ROLE', 'X-OpenWebUI-User-Role')
FORWARD_SESSION_INFO_HEADER_MESSAGE_ID = get_env('FORWARD_SESSION_INFO_HEADER_MESSAGE_ID', 'X-OpenWebUI-Message-Id')
FORWARD_SESSION_INFO_HEADER_CHAT_ID = get_env('FORWARD_SESSION_INFO_HEADER_CHAT_ID', 'X-OpenWebUI-Chat-Id')

# If set while ENABLE_FORWARD_USER_INFO_HEADERS is True, send one signed HS256 JWT
# (FORWARD_USER_INFO_HEADER_JWT) instead of separate X-OpenWebUI-User-* headers.
FORWARD_USER_INFO_HEADER_JWT_SECRET = (os.environ.get('FORWARD_USER_INFO_HEADER_JWT_SECRET') or '').strip() or None
FORWARD_USER_INFO_HEADER_JWT = os.environ.get('FORWARD_USER_INFO_HEADER_JWT', 'X-OpenWebUI-User-Jwt')
try:
    FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS = int(
        os.environ.get('FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS', '300')
    )
except ValueError:
    FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS = 300

####################################
# Progressive Web App
####################################

EXTERNAL_PWA_MANIFEST_URL = get_env('EXTERNAL_PWA_MANIFEST_URL', None)

####################################
# GROUP DEFAULTS
####################################

# Controls the default "Who can share to this group" setting for new groups.
# Env var values: "true" (anyone), "false" (no one), "members" (only group members).
_default_group_share = get_env('DEFAULT_GROUP_SHARE_PERMISSION', 'members').strip().lower()
DEFAULT_GROUP_SHARE_PERMISSION = 'members' if _default_group_share == 'members' else _default_group_share == 'true'

####################################
# MODELS
####################################

ENABLE_CUSTOM_MODEL_FALLBACK = get_env('ENABLE_CUSTOM_MODEL_FALLBACK', 'False').lower() == 'true'

MODELS_CACHE_TTL = get_env('MODELS_CACHE_TTL', '1')
if MODELS_CACHE_TTL == '':
    MODELS_CACHE_TTL = None
else:
    try:
        MODELS_CACHE_TTL = int(MODELS_CACHE_TTL)
    except Exception:
        MODELS_CACHE_TTL = 1


####################################
# CHAT
####################################

ENABLE_CHAT_RESPONSE_BASE64_IMAGE_URL_CONVERSION = (
    get_env('ENABLE_CHAT_RESPONSE_BASE64_IMAGE_URL_CONVERSION', 'False').lower() == 'true'
)

# When enabled, uses a hardcoded extension-to-MIME dictionary as a last-resort
# fallback when both mimetypes.guess_type() and file.meta.content_type fail to
# determine the content type. This can help on minimal container images (e.g.
# wolfi-base) that lack /etc/mime.types AND have legacy files without stored
# content_type metadata.
ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK = (
    get_env('ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK', 'False').lower() == 'true'
)

CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = get_env('CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE', '1')

if CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE == '':
    CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = 1
else:
    try:
        CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = int(CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE)
    except Exception:
        CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = 1


# Maximum tool-call iterations per chat response. Set to -1 for unlimited.
# The old CHAT_RESPONSE_MAX_TOOL_CALL_RETRIES name is accepted as a fallback.
CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS = get_env(
    'CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS',
    get_env('CHAT_RESPONSE_MAX_TOOL_CALL_RETRIES', '256'),
)

if CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS == '':
    CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS = 256
else:
    try:
        CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS = int(CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS)
    except Exception:
        CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS = 256

# -1 means unlimited (no cap).
if CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS == -1:
    CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS = None


# WARNING: Experimental. Only enable if your upstream Responses API endpoint
# supports stateful sessions (i.e. server-side response storage with
# previous_response_id anchoring). Most proxies and third-party endpoints
# are stateless and will break if this is enabled.
ENABLE_RESPONSES_API_STATEFUL = get_env('ENABLE_RESPONSES_API_STATEFUL', 'False').lower() == 'true'


CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE = get_env('CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE', '')

if CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE == '':
    CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE = None
else:
    try:
        CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE = int(CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE)
    except Exception:
        CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE = None


####################################
# SENTENCE TRANSFORMERS
####################################
# Removed: local embeddings/reranking are not used in this deployment, the
# embedding API (RAG_EMBEDDING_ENGINE=openai) is used instead.

# Whether to apply sigmoid normalization to CrossEncoder reranking scores.
# When enabled (default), scores are normalized to 0-1 range for proper
# relevance threshold behavior with MS MARCO models.
SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION = (
    get_env('SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION', 'True').lower() == 'true'
)

####################################
# TOOLS/FUNCTIONS PIP OPTIONS
####################################

ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS = (
    get_env('ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS', 'True').lower() == 'true'
)

PIP_OPTIONS = get_env('PIP_OPTIONS', '').split()
PIP_PACKAGE_INDEX_OPTIONS = get_env('PIP_PACKAGE_INDEX_OPTIONS', '').split()


####################################
# OFFLINE_MODE
####################################

ENABLE_VERSION_UPDATE_CHECK = get_env('ENABLE_VERSION_UPDATE_CHECK', 'true').lower() == 'true'
OFFLINE_MODE = get_env('OFFLINE_MODE', 'false').lower() == 'true'

if OFFLINE_MODE:
    os.environ['HF_HUB_OFFLINE'] = '1'
    ENABLE_VERSION_UPDATE_CHECK = False

####################################
# Audit logging
####################################


ENABLE_AUDIT_STDOUT = get_env('ENABLE_AUDIT_STDOUT', 'False').lower() == 'true'
ENABLE_AUDIT_LOGS_FILE = get_env('ENABLE_AUDIT_LOGS_FILE', 'True').lower() == 'true'

# Where to store log file
# Defaults to the DATA_DIR/audit.log. To set AUDIT_LOGS_FILE_PATH you need to
# provide the whole path, like: /app/audit.log
AUDIT_LOGS_FILE_PATH = get_env('AUDIT_LOGS_FILE_PATH', f'{DATA_DIR}/audit.log')
# Maximum size of a file before rotating into a new log file
AUDIT_LOG_FILE_ROTATION_SIZE = get_env('AUDIT_LOG_FILE_ROTATION_SIZE', '10MB')

# Comma separated list of logger names to use for audit logging
# Default is "uvicorn.access" which is the access log for Uvicorn
# You can add more logger names to this list if you want to capture more logs
AUDIT_UVICORN_LOGGER_NAMES = get_env('AUDIT_UVICORN_LOGGER_NAMES', 'uvicorn.access').split(',')

# METADATA | REQUEST | REQUEST_RESPONSE
AUDIT_LOG_LEVEL = get_env('AUDIT_LOG_LEVEL', 'NONE').upper()
try:
    MAX_BODY_LOG_SIZE = int(get_env('MAX_BODY_LOG_SIZE') or 2048)
except ValueError:
    MAX_BODY_LOG_SIZE = 2048

# Comma separated list for urls to exclude from audit
AUDIT_EXCLUDED_PATHS = get_env('AUDIT_EXCLUDED_PATHS', '/chats,/chat,/folders').split(',')
AUDIT_EXCLUDED_PATHS = [path.strip() for path in AUDIT_EXCLUDED_PATHS]
AUDIT_EXCLUDED_PATHS = [path.lstrip('/') for path in AUDIT_EXCLUDED_PATHS]

# Comma separated list of urls to include in audit (whitelist mode)
# When set, only these paths are audited and AUDIT_EXCLUDED_PATHS is ignored
AUDIT_INCLUDED_PATHS = get_env('AUDIT_INCLUDED_PATHS', '').split(',')
AUDIT_INCLUDED_PATHS = [path.strip() for path in AUDIT_INCLUDED_PATHS]
AUDIT_INCLUDED_PATHS = [path.lstrip('/') for path in AUDIT_INCLUDED_PATHS if path]

# When enabled, GET requests are also audited (disabled by default to avoid log noise)
ENABLE_AUDIT_GET_REQUESTS = get_env('ENABLE_AUDIT_GET_REQUESTS', 'False').lower() == 'true'


####################################
# OPENTELEMETRY
####################################

ENABLE_OTEL = get_env('ENABLE_OTEL', 'False').lower() == 'true'
ENABLE_OTEL_TRACES = get_env('ENABLE_OTEL_TRACES', 'False').lower() == 'true'
ENABLE_OTEL_METRICS = get_env('ENABLE_OTEL_METRICS', 'False').lower() == 'true'
ENABLE_OTEL_LOGS = get_env('ENABLE_OTEL_LOGS', 'False').lower() == 'true'

OTEL_EXPORTER_OTLP_ENDPOINT = get_env('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://localhost:4317')
OTEL_METRICS_EXPORTER_OTLP_ENDPOINT = get_env('OTEL_METRICS_EXPORTER_OTLP_ENDPOINT', OTEL_EXPORTER_OTLP_ENDPOINT)
OTEL_LOGS_EXPORTER_OTLP_ENDPOINT = get_env('OTEL_LOGS_EXPORTER_OTLP_ENDPOINT', OTEL_EXPORTER_OTLP_ENDPOINT)
OTEL_EXPORTER_OTLP_INSECURE = get_env('OTEL_EXPORTER_OTLP_INSECURE', 'False').lower() == 'true'
OTEL_METRICS_EXPORTER_OTLP_INSECURE = (
    get_env('OTEL_METRICS_EXPORTER_OTLP_INSECURE', str(OTEL_EXPORTER_OTLP_INSECURE)).lower() == 'true'
)
OTEL_LOGS_EXPORTER_OTLP_INSECURE = (
    get_env('OTEL_LOGS_EXPORTER_OTLP_INSECURE', str(OTEL_EXPORTER_OTLP_INSECURE)).lower() == 'true'
)
OTEL_SERVICE_NAME = get_env('OTEL_SERVICE_NAME', 'open-webui')
OTEL_RESOURCE_ATTRIBUTES = get_env('OTEL_RESOURCE_ATTRIBUTES', '')  # e.g. key1=val1,key2=val2
OTEL_TRACES_SAMPLER = get_env('OTEL_TRACES_SAMPLER', 'parentbased_always_on').lower()
OTEL_BASIC_AUTH_USERNAME = get_env('OTEL_BASIC_AUTH_USERNAME', '')
OTEL_BASIC_AUTH_PASSWORD = get_env('OTEL_BASIC_AUTH_PASSWORD', '')
OTEL_METRICS_EXPORT_INTERVAL_MILLIS = int(get_env('OTEL_METRICS_EXPORT_INTERVAL_MILLIS', '10000'))

OTEL_METRICS_BASIC_AUTH_USERNAME = get_env('OTEL_METRICS_BASIC_AUTH_USERNAME', OTEL_BASIC_AUTH_USERNAME)
OTEL_METRICS_BASIC_AUTH_PASSWORD = get_env('OTEL_METRICS_BASIC_AUTH_PASSWORD', OTEL_BASIC_AUTH_PASSWORD)
OTEL_LOGS_BASIC_AUTH_USERNAME = get_env('OTEL_LOGS_BASIC_AUTH_USERNAME', OTEL_BASIC_AUTH_USERNAME)
OTEL_LOGS_BASIC_AUTH_PASSWORD = get_env('OTEL_LOGS_BASIC_AUTH_PASSWORD', OTEL_BASIC_AUTH_PASSWORD)

OTEL_OTLP_SPAN_EXPORTER = get_env('OTEL_OTLP_SPAN_EXPORTER', 'grpc').lower()  # grpc or http

OTEL_METRICS_OTLP_SPAN_EXPORTER = get_env(
    'OTEL_METRICS_OTLP_SPAN_EXPORTER', OTEL_OTLP_SPAN_EXPORTER
).lower()  # grpc or http

OTEL_LOGS_OTLP_SPAN_EXPORTER = get_env(
    'OTEL_LOGS_OTLP_SPAN_EXPORTER', OTEL_OTLP_SPAN_EXPORTER
).lower()  # grpc or http
