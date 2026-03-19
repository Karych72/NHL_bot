import os
import getpass
from datetime import date

_today = date.today().isoformat()

def _env(name: str, default: str) -> str:
    value = os.getenv(name, default)
    return value if value else default


DATE_FROM = _env("DATE_FROM", _today)
DATE_TO = _env("DATE_TO", _today)
PG_PORT = _env("PG_PORT", "5432")
PG_HOST = _env("PG_HOST", "localhost")
PG_USER = _env("PG_USER", getpass.getuser())
PG_DATABASE = _env("PG_DATABASE", "postgres")

ROSTER_DIR = '../all_data/dataframes/rosters'
TEAMS_ROSTER_URL = 'https://statsapi.web.nhl.com/api/v1/people/'
PLAYERS_DIR = '../all_data/dataframes/players'
REQUEST_SUFFIX = '/stats?stats=statsSingleSeason&season='
SEASON = _env("SEASON", "20222023")

TEAMS_URL = 'https://statsapi.web.nhl.com/api/v1/teams/'
ROSTERS_DF_DIR = '../all_data/dataframes/rosters'
TEAMS_DF_DIR = '../all_data/dataframes/teams'

DATAFRAMES_DIR = '../all_data/dataframes/myself_analyses'
JSON_DIR = '../all_data/dataframes/myself_analyses'

CURRENT_SEASON = _env("CURRENT_SEASON", "22/23")
