import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


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
