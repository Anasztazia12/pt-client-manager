import os

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

    # For deployment: set DATABASE_URL to a MySQL connection string, e.g.
    # mysql+pymysql://USER:PASSWORD@HOST:3306/DBNAME
    # For local development, falls back to a SQLite file so you can run
    # and test everything without a MySQL server installed.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'dev.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Stripe: leave the secret key unset and the app runs with payments switched off.
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_CURRENCY = os.environ.get("STRIPE_CURRENCY", "gbp").lower()

    # Address Stripe sends paying clients back to. Render provides RENDER_EXTERNAL_URL itself;
    # without either, the address of the current request is used.
    PUBLIC_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""

    # Public try-out mode: fills an empty database with sample clients and shows a banner.
    DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")
