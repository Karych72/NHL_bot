import pandas as pd
import requests
import time
import config
from postgres_nhl import delete_days, insert_pg, truncate_table

start = time.time()

ROSTER_DIR = config.ROSTER_DIR
TEAMS_ROSTER_URL = config.TEAMS_ROSTER_URL
PLAYERS_DIR = config.PLAYERS_DIR
REQUEST_SUFFIX = config.REQUEST_SUFFIX
SEASON = config.SEASON

TEAMS_URL = config.TEAMS_URL
ROSTERS_DF_DIR = config.ROSTERS_DF_DIR
TEAMS_DF_DIR = config.TEAMS_DF_DIR

ALL_ID_TEAMS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
                12, 13, 14, 15, 16, 17, 18, 19, 20,
                21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
                52, 53, 54, 55]


def get_short_name(name: str) -> str:
    if 'Detroit' in name:
        return 'Detroit'
    if 'Rangers' in name:
        return 'Rangers'
    if 'Islanders' in name:
        return 'Islanders'
    if "Columbus Blue" in name:
        return 'Columbus'
    if 'Vegas' in name:
        return 'Vegas'
    if 'Toronto' in name:
        return 'Toronto'
    return ' '.join(name.split(' ')[:-1])


def get_team(team_id: int):
    request = TEAMS_URL + str(team_id)
    team_request = requests.get(request).json()
    team = team_request['teams'][0]
    short_name = get_short_name(team['name'])
    return {'team_id': team_id,
            'name': team['name'],
            'division_name': team['division']['name'],
            'arena': team['venue']['name'],
            'conference_name': team['conference']['name'],
            'abbreviation': team['abbreviation'],
            'firstYearOfPlay': team['firstYearOfPlay'],
            'city': team['locationName'],
            'active': team['active'],
            'short_name': short_name
            }


def get_player_info(player_id: int):
    request = TEAMS_ROSTER_URL + str(player_id)

    player_info = requests.get(request).json()
    return {'currentAge': player_info['people'][0]['currentAge'],
            'lastName': player_info['people'][0]['lastName'],
            'nationality': player_info['people'][0]['nationality'],
            'captain': player_info['people'][0]['captain'],
            'alternateCaptain': player_info['people'][0]['alternateCaptain'],
            'rookie': player_info['people'][0]['rookie'],
            'abbreviation': player_info['people'][0]['primaryPosition']['abbreviation']
            }


def get_roster_team(team_id: int):
    request = TEAMS_URL + str(team_id) + '/roster'
    team_request = requests.get(request).json()
    roster_team = []
    players_id = []
    for player in team_request['roster']:

        players_id.append([player['person']['id'], player['position']['code']])

        t = {'player_id': player['person']['id'],
             'name': player['person']['fullName'],
             'position': player['position']['code'],
             'jersey_number': player['jerseyNumber']}

        t.update(get_player_info(t['player_id']))
        t['current_team_id'] = team_id
        roster_team.append(t)

    pd.DataFrame(roster_team).to_csv(f'{ROSTERS_DF_DIR}/{team_id}.csv', index=False)

    insert_pg('rosters', pd.DataFrame(roster_team))
    print(f'Save {team_id} roster!')
    return players_id


def get_player_stats(player_id: int):
    request = TEAMS_ROSTER_URL + str(player_id) + f'{REQUEST_SUFFIX}{SEASON}'
    team_request = requests.get(request).json()
    return team_request


def get_team_stats(team_id: int) -> dict:
    request = TEAMS_URL + str(team_id) + '/stats'
    team_request = requests.get(request).json()
    need_key = ['gamesPlayed', 'wins', 'losses', 'ot', 'pts', 'ptPctg', 'goalsPerGame',
                'goalsAgainstPerGame', 'powerPlayPercentage', 'powerPlayGoals',
                'powerPlayGoalsAgainst', 'powerPlayOpportunities',
                'penaltyKillPercentage', 'shotsPerGame', 'shotsAllowed', 'faceOffWinPercentage']
    # print(team_request)
    stats = team_request['stats'][0]['splits'][0]['stat']
    stats['powerPlayGoals'] = int(stats['powerPlayGoals'])
    stats['powerPlayGoalsAgainst'] = int(stats['powerPlayGoalsAgainst'])
    stats['powerPlayOpportunities'] = int(stats['powerPlayOpportunities'])
    team_stats = {'team_id': team_id}
    team_stats.update({key: stats[key] for key in need_key})

    return team_stats


def get_players_goalies_stats(pl):
    players_id = [player[0] for player in pl if player[1] != 'G']
    goalies_id = [player[0] for player in pl if player[1] == 'G']

    all_players = []
    all_goalies = []

    for player_id in players_id:
        try:
            player = get_player_stats(player_id)['stats'][0]['splits'][0]['stat']
            player['player_id'] = player_id
            all_players.append(player)
        except:
            print(f"player id {player_id} doesn't exist")

    for goalie_id in goalies_id:
        try:
            goalies = get_player_stats(goalie_id)['stats'][0]['splits'][0]['stat']
            goalies['player_id'] = goalie_id
            all_goalies.append(goalies)
        except:
            print(f"goalie id {goalie_id} doesn't exist")
    return all_players, all_goalies


if __name__ == "__main__":
    all_df = []
    players_id = []
    teams_stats = []
    truncate_table('goalies_season_stats')
    truncate_table('players_season_stats')
    truncate_table('teams_stats')
    truncate_table('rosters')
    truncate_table('teams')
    for i in ALL_ID_TEAMS:
        try:
            now_team = get_team(i)
            print(now_team['name'], i)
            all_df.append(now_team)
        except:
            print(f"id: {i} append_now_team is wrong")
        try:
            players_id.extend(get_roster_team(i))
        except:
            print(f"id: {i} extend_roster_team is wrong")
        try:
            teams_stats.append(get_team_stats(i))
        except:
            print(f"id: {i} get_team_stats is wrong")

    pd.DataFrame(all_df).to_csv(f'{TEAMS_DF_DIR}/teams_main.csv', index=False)
    print('Loaded Teams')
    insert_pg('teams', pd.DataFrame(all_df))
    print('Teams are loaded')
    end = time.time()
    print(end - start)

    insert_pg('teams_stats', pd.DataFrame(teams_stats))

    players, goalies = get_players_goalies_stats(players_id)
    pd.DataFrame(players).to_csv(f'{PLAYERS_DIR}/players.csv', index=False)
    pd.DataFrame(goalies).to_csv(f'{PLAYERS_DIR}/goalies.csv', index=False)

    print('data loading...')
    insert_pg('players_season_stats', pd.DataFrame(players))
    print('players has loaded!')

    insert_pg('goalies_season_stats', pd.DataFrame(goalies))
    print('all goalies has loaded!')
    print('All data has loaded!')
