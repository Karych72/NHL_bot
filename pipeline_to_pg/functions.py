import requests
from functools import reduce
from operator import add


def get_game(game_id: int):
    request = 'https://statsapi.web.nhl.com/api/v1/game/{}/feed/live'.format(game_id)
    return requests.get(request).json()['liveData']


def column_by_create(string: str) -> set:
    all_rows = string.split('\n')
    result = []
    for row in all_rows:
        words = row.split(' ')
        for word in words:
            if word != '':
                result.append(word)
                break
    return set(result)


def get_time_goal(period: int, time: str) -> str:
    if period == 1:
        return time
    else:
        minuet = (period - 1) * 20
        time_period_min = int(time[0:2])
        return str(minuet + time_period_min) + time[2:5]


def print_scores(game: dict) -> None:
    for event in game['plays']['allPlays']:
        if event['result']['event'] == 'Goal':
            print(game_goals(event))


def goal_stat(event: dict, game_id=0, is_print=False) -> dict:
    # global bad_en, good_en
    scorer = 'Scorer'
    assist = 'Assist'
    s = ''
    res_dict = {}
    num_assists = 0
    for player in event['players']:
        if player['playerType'] == scorer:
            s = s + player['player']['fullName'].split(' ')[-1] + '(' + str(player['seasonTotal']) + ') - '
            res_dict['goal_player_id'] = player['player']['id']
            res_dict['total_goals'] = player['seasonTotal']
        if player['playerType'] == assist:
            num_assists += 1
            s = s + player['player']['fullName'].split(' ')[-1] + '(' + str(player['seasonTotal']) + '), '
            res_dict['assist_player' + str(num_assists) + '_id'] = int(player['player']['id'])
            res_dict['assist_total' + '_' + str(num_assists)] = int(player['seasonTotal'])
    s = s + get_time_goal(event['about']['period'], event['about']['periodTime']) + ', '
    s = s + str(event['about']['goals']['away']) + ':' + str(event['about']['goals']['home']) + ' '
    try:
        if event['result']['emptyNet']:
            s = s + 'EN '
            res_dict['empty_net'] = True
        else:
            res_dict['empty_net'] = False
        # good_en += 1
    except KeyError:
        res_dict['empty_net'] = False
        # bad_en += 1
    if event['result']['gameWinningGoal']:
        s = s + 'GWG '
        res_dict['winner_goal'] = True
    else:
        res_dict['winner_goal'] = False
    res_dict['is_ppg'] = event['result']['strength']['code'] == 'PPG'
    res_dict['is_shg'] = event['result']['strength']['code'] == 'SHG'
    res_dict['team_id'] = event['team']['id']
    res_dict['game_id'] = game_id
    if is_print:
        print(s)
    return res_dict


def game_goals(game: dict, game_id=0) -> list:
    all_goals = []
    for event in game['plays']['allPlays']:
        if event['result']['event'] == 'Goal':
            all_goals.append(goal_stat(event, game_id, is_print=False))
    return all_goals


def team_stats(game: dict, is_home: bool, game_id=0) -> dict:
    if is_home:
        field = 'home'
    else:
        field = 'away'
    result = game['boxscore']['teams'][field]['teamStats']['teamSkaterStats']
    periods = [period[field]['goals'] for period in game['linescore']['periods']]
    result.update({'game_id': game_id,
                   'team_id': game['boxscore']['teams'][field]['team']['id'],
                   'fst_period_goals': periods[0],
                   'snd_period_goals': periods[0],
                   'trd_period_goals': periods[0]})
    return result


def game_stats(game: dict, game_id=0) -> dict:
    away_id = game['boxscore']['teams']['away']['team']['id']
    home_id = game['boxscore']['teams']['home']['team']['id']
    is_overtime = game['linescore']['currentPeriod'] > 3

    periods = [[period['home']['goals'], period['away']['goals']]
               for period in game['linescore']['periods']]
    day = str(game['plays']['allPlays'][0]['about']['dateTime'])[0:10]

    return {
        'game_id': game_id,
        'day': day,
        'home_team_id': home_id,
        'away_team_id': away_id,
        'winner_team_id': home_id if game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] > game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] else away_id,
        'lose_team_id': away_id if game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] > game['boxscore']['teams']['home']['teamStats']['teamSkaterStats']['goals'] else home_id,
        'is_overtime': is_overtime,
        'is_shootouts': False,
        'season': '19/20'}


def player_stats(game: dict, is_home: str, game_id=0) -> dict:
    goalie = []
    players = []
    if is_home:
        field = 'home'
    else:
        field = 'away'
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
    game_ids = reduce(add, [[game['gamePk'] for game in date['games']] for date in schedule], [])
    all_games = {game_id: get_game(game_id) for game_id in game_ids}
    return {game_id: {'all_goals': game_goals(all_games[game_id], game_id),
                      'game_team_stats_home': team_stats(all_games[game_id], True, game_id),
                      'game_team_stats_away': team_stats(all_games[game_id], False, game_id),
                      'game_player_stats_home': player_stats(all_games[game_id], 'home', game_id)['players'],
                      'game_goalie_stats_home': player_stats(all_games[game_id], 'home', game_id)['goalie'],
                      'game_player_stats_away': player_stats(all_games[game_id], 'away', game_id)['players'],
                      'game_goalie_stats_away': player_stats(all_games[game_id], 'away', game_id)['goalie'],
                      'game_stats': game_stats(all_games[game_id], game_id)} for game_id in game_ids}
