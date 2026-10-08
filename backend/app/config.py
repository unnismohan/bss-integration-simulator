import os
from dotenv import load_dotenv

load_dotenv()


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://simulator:simulator@localhost:5432/simulator",
)
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
SIMULATOR_API_KEY = os.getenv("SIMULATOR_API_KEY", "")
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "true" if APP_ENV != "production" else "false").lower() in {"1", "true", "yes"}
CALLBACK_ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.getenv("CALLBACK_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
}
CALLBACK_POLL_SECONDS = float(os.getenv("CALLBACK_POLL_SECONDS", "0.25"))
CALLBACK_BATCH_SIZE = int(os.getenv("CALLBACK_BATCH_SIZE", "50"))
CALLBACK_TIMEOUT_SECONDS = float(os.getenv("CALLBACK_TIMEOUT_SECONDS", "5"))
CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if origin.strip()]
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "1048576"))
API_DB_POOL_SIZE = int(os.getenv("API_DB_POOL_SIZE", "20"))
API_DB_MAX_OVERFLOW = int(os.getenv("API_DB_MAX_OVERFLOW", "20"))

if MAX_REQUEST_BYTES < 1:
    raise ValueError("MAX_REQUEST_BYTES must be positive")
if CALLBACK_POLL_SECONDS <= 0 or CALLBACK_TIMEOUT_SECONDS <= 0:
    raise ValueError("Callback polling interval and timeout must be positive")
if CALLBACK_BATCH_SIZE < 1:
    raise ValueError("CALLBACK_BATCH_SIZE must be positive")
if API_DB_POOL_SIZE < 1 or API_DB_MAX_OVERFLOW < 0:
    raise ValueError("Database pool size must be positive and max overflow cannot be negative")
