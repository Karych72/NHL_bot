import os

DATE_FROM = "2022-12-10"
DATE_TO = "2022-12-10"
PG_PORT = "5432"
PG_HOST = "localhost"
# PG_PASSWORD = os.environ["PG_DOCS_PASSWORD"]
PG_USER = "petrkarol"
PG_DATABASE = "postgres"

ROSTER_DIR = '../all_data/dataframes/rosters'
TEAMS_ROSTER_URL = 'https://statsapi.web.nhl.com/api/v1/people/'
PLAYERS_DIR = '../all_data/dataframes/players'
REQUEST_SUFFIX = '/stats?stats=statsSingleSeason&season='
SEASON = '20222023'

TEAMS_URL = 'https://statsapi.web.nhl.com/api/v1/teams/'
ROSTERS_DF_DIR = '../all_data/dataframes/rosters'
TEAMS_DF_DIR = '../all_data/dataframes/teams'

DATAFRAMES_DIR = '../all_data/dataframes/myself_analyses'
JSON_DIR = '../all_data/dataframes/myself_analyses'

CURRENT_SEASON = '22/23'
