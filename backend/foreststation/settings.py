"""
Forest research station — Django settings.

Database strategy
------------------
Production: PostgreSQL + PostGIS. Geometry columns are created with raw SQL
(see ``deploy/postgis.sql``) because the development environment in this
repository runs on the stock sqlite3 backend (no GDAL/GEOS system libraries).
All model tables are therefore portable; PostGIS adds generated geometry
columns, GiST indexes and the boundary area CHECK on top of the same schema.

Switch backends with FOREST_DB=postgis (see DATABASES below).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "FOREST_SECRET_KEY",
    "dev-only-key-replaced-by-env-in-production",
)
DEBUG = os.environ.get("FOREST_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rest_framework",
    "inventory",
]

MIDDLEWARE = []

ROOT_URLCONF = "foreststation.urls"
WSGI_APPLICATION = "foreststation.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

_db_engine = os.environ.get("FOREST_DB", "sqlite")
if _db_engine == "postgis":
    DATABASES = {
        "default": {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "NAME": os.environ.get("PGDATABASE", "foreststation"),
            "USER": os.environ.get("PGUSER", "forest"),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# --- Domain configuration ---------------------------------------------------
# Coordinates are stored in a projected CRS; distances/areas come out in
# metres / square metres. The EPSG code travels with every API payload so the
# unit contract is explicit rather than implied.
SURVEY_CRS_EPSG = int(os.environ.get("FOREST_CRS_EPSG", "32650"))  # UTM 50N

# Recruitment threshold: trees with dbh >= this (cm) at t2 are "in" the
# remeasured population. Smaller stems are recorded but not counted as
# ingrowth. Kept in the estimate-version design snapshot.
RECRUITMENT_DBH_CM = float(os.environ.get("FOREST_RECRUIT_DBH_CM", "5.0"))

# Measurement protocol uncertainties (one standard deviation of the
# instrument/observer error). Explicitly recorded, propagated into biomass.
DBH_MEASUREMENT_SD_CM = 0.10
HEIGHT_MEASUREMENT_SD_M = 0.30

# |dbh_t2 - dbh_t1| at or below this is reported as verified zero growth
# (re-measured with cross-check), never silently rounded to zero.
ZERO_GROWTH_TOL_CM = 0.15

# Declared plot area vs polygon area may diverge by this relative amount
# (handheld-GPS boundary noise). Beyond it the import fails.
PLOT_AREA_TOLERANCE = 0.01
