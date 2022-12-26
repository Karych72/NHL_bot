import requests
from functools import reduce
from operator import add
import pandas as pd
import json
import config


def get_game(game_id: int):
    # 2022020441
    request = 'https://statsapi.web.nhl.com/api/v1/game/{}/feed/live'.format(game_id)
    print(game_id)
    return requests.get(request).json()['liveData']
# https://statsapi.web.nhl.com/api/v1/game/2022020434/feed/live


# print(get_game(2022020081))


def get_time_goal(period: int, time: str) -> str:
    """Get row of time"""
    if period == 1:
        return time
    else:
        minute = (period - 1) * 20
        time_period_min = int(time[0:2])
        return str(minute + time_period_min) + time[2:5]


def goal_stat(event: dict, game_id=0) -> dict:
    """stat of goal: scorer, assist and other"""
    scorer = 'Scorer'
    assist = 'Assist'

    res_dict = {}
    num_assists = 0
    for player in event['players']:
        if player['playerType'] == scorer:
            res_dict['goal_player_id'] = player['player']['id']
            res_dict['total_goals'] = player['seasonTotal']
        if player['playerType'] == assist:
            num_assists += 1
            res_dict['assist_player' + str(num_assists) + '_id'] = int(player['player']['id'])
            res_dict['assist_total' + '_' + str(num_assists)] = int(player['seasonTotal'])
    try:
        if event['result']['emptyNet']:
            res_dict['empty_net'] = True
        else:
            res_dict['empty_net'] = False
    except KeyError:
        res_dict['empty_net'] = False
    if event['result']['gameWinningGoal']:
        res_dict['winner_goal'] = True
    else:
        res_dict['winner_goal'] = False

    res_dict.update({'is_ppg': event['result']['strength']['code'] == 'PPG',
                     'is_shg': event['result']['strength']['code'] == 'SHG',
                     'team_id': event['team']['id'],
                     'game_id': game_id,
                     'period': event['about']['period'],
                     'time': event['about']['periodTime'],
                     'goals_home': event['about']['goals']['home'],
                     'goals_away': event['about']['goals']['away']}
                    )

    return res_dict


def game_goals(game: dict, game_id=0) -> list:
    """all goals in the game"""
    all_goals = []
    for event in game['plays']['allPlays']:
        if event['result']['event'] == 'Goal':
            all_goals.append(goal_stat(event, game_id))
    return all_goals


def team_stats(game: dict, is_home: bool, game_id=0) -> dict:
    """get team statistic on the game"""
    if is_home:
        field = 'home'
    else:
        field = 'away'
    result = game['boxscore']['teams'][field]['teamStats']['teamSkaterStats']
    periods = [period[field]['goals'] for period in game['linescore']['periods']]
    result.update({'game_id': game_id,
                   'team_id': game['boxscore']['teams'][field]['team']['id'],
                   'field': field,
                   'fst_period_goals': periods[0],
                   'snd_period_goals': periods[1],
                   'trd_period_goals': periods[2]})
    return result


def game_stats(game: dict, day: str, game_id=0) -> dict:
    """get game statistic"""
    away_id = game['boxscore']['teams']['away']['team']['id']
    home_id = game['boxscore']['teams']['home']['team']['id']
    is_overtime = game['linescore']['currentPeriod'] > 3

    return {
        'game_id': game_id,
        'day': day,
        'home_team_id': home_id,
        'away_team_id': away_id,
        'winner_team_id': home_id if game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] > game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] else away_id,
        'lose_team_id': away_id if game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] > game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] else home_id,
        'is_overtime': is_overtime,
        'is_shootouts': False,
        'season': config.CURRENT_SEASON}


def player_stats(game: dict, field: str, game_id=0) -> dict:
    """get player statistic on the game"""
    goalie = []
    players = []
    for player in game['boxscore']['teams'][field]['players']:
        boxcore_player = game['boxscore']['teams'][field]['players'][player]
        players_stat = {
            'team_id': game['boxscore']['teams'][field]['team']['id'],
            'game_id': game_id,
            'player_id': boxcore_player['person']['id']
        }
        if 'goalieStats' in boxcore_player['stats']:
            players_stat.update(boxcore_player['stats']['goalieStats'])
            goalie.append(players_stat)
        else:
            try:
                players_stat.update(boxcore_player['stats']['skaterStats'])
                players.append(players_stat)
            except KeyError:
                pass
    return {'players': players, 'goalie': goalie}


def get_schedule(start_date, end_date) -> dict:
    request = 'https://statsapi.web.nhl.com/api/v1/schedule?startDate={}&endDate={}'.format(start_date, end_date)
    return requests.get(request).json()


def get_games_df(start_date, end_date) -> dict:
    schedule = get_schedule(start_date, end_date)['dates']
    game_ids = reduce(add, [[(game['gamePk'], date['date']) for game in date['games']] for date in schedule], [])
    all_games = {game_id[0]: get_game(game_id[0]) for game_id in game_ids}
    return {game_id[0]: {'all_goals': game_goals(all_games[game_id[0]], game_id[0]),
                         'game_team_stats_home': team_stats(all_games[game_id[0]], True, game_id[0]),
                         'game_team_stats_away': team_stats(all_games[game_id[0]], False, game_id[0]),
                         'game_player_stats_home': player_stats(all_games[game_id[0]], 'home', game_id[0])['players'],
                         'game_goalie_stats_home': player_stats(all_games[game_id[0]], 'home', game_id[0])['goalie'],
                         'game_player_stats_away': player_stats(all_games[game_id[0]], 'away', game_id[0])['players'],
                         'game_goalie_stats_away': player_stats(all_games[game_id[0]], 'away', game_id[0])['goalie'],
                         'game_stats': game_stats(all_games[game_id[0]], game_id[1], game_id[0])}
            for game_id in game_ids}


def player_team_stats_to_json(df: pd.DataFrame, second_key: str) -> json:
    """
    Second key for players is "player_id"
    Second key for team_stats is "field"
    """
    to_json = json.loads(df.to_json(orient="index"))
    result_js = {}

    for key in to_json:
        now_game_id = to_json[key]['game_id']
        now_field = to_json[key][second_key]
        if now_game_id not in result_js.keys():
            result_js[now_game_id] = {}
        result_js[now_game_id][now_field] = to_json[key]
    return result_js


def game_and_goals_stats_to_json(df: pd.DataFrame) -> json:
    df = df.reset_index(drop=True)
    to_json = json.loads(df.to_json(orient="index"))
    result_js = {}

    for key in to_json:
        now_game_id = to_json[key]['game_id']
        if now_game_id not in result_js.keys():
            result_js[now_game_id] = []
        result_js[now_game_id].append(to_json[key])
    return result_js


def write_json(obj, name_file):
    with open(f"{name_file}.json", "w") as outfile:
        json.dump(obj, outfile)
