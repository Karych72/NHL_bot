from psycopg2 import sql

from database import fetch_all, validate_table, validate_column
from template_funcs import output_text


def game_message(game_id: int) -> str:
    game_stats = fetch_all(
        "SELECT * FROM get_game_stats(%s)", (game_id,),
        ['goals', 'pim', 'blocks', 'hits', 'shots', 'is_overtime',
         'is_shootout', 'field', 'team_name'],
    )
    game_goals = fetch_all(
        "SELECT * FROM get_goals_game(%s)", (game_id,),
        ['scorer', 'assist_1', 'assist_2', 'period', 'time',
         'home_score', 'away_score'],
    )
    game_goalies = fetch_all(
        "SELECT * FROM get_goalies_game(%s)", (game_id,),
        ['shots', 'saves', 'timeonice', 'lastname',
         'save_percentage', 'is_home'],
    )

    if not game_stats['is_overtime'][0]:
        extra = ''
    elif not game_stats['is_shootout'][0]:
        extra = '(OT)'
    else:
        extra = '(Б)'

    goals = []
    for i in range(game_goals['count_rows']):
        scorer = game_goals['scorer'][i] or 'Unknown'
        t_m = str((game_goals['period'][i] - 1) * 20 + int(game_goals['time'][i].split(':')[0]))
        t_all = t_m + ':' + game_goals['time'][i].split(':')[1]
        assists = ''
        if game_goals['assist_2'][i] is not None:
            assists = f"({game_goals['assist_1'][i]}, {game_goals['assist_2'][i]})"
        elif game_goals['assist_1'][i] is not None:
            assists = f"({game_goals['assist_1'][i]})"
        goals.append({'home_score': game_goals['home_score'][i],
                      'away_score': game_goals['away_score'][i],
                      'scorer': scorer + assists,
                      'time': t_all
                      })

    goalkeepers = ''
    change_team = False
    for i in range(game_goalies['count_rows']):
        if not game_goalies['is_home'][i] and not change_team:
            change_team = True
            goalkeepers += ' - '
        goalkeepers += (
            f"{game_goalies['lastname'][i]} "
            f"({game_goalies['saves'][i]}/{game_goalies['shots'][i]}, "
            f"{round(game_goalies['save_percentage'][i], 2)}%, "
            f"{game_goalies['timeonice'][i]})"
        )

    to_template = {
        'team_home': game_stats['team_name'][0],
        'team_away': game_stats['team_name'][1],
        'home_score': game_stats['goals'][0],
        'away_score': game_stats['goals'][1],
        'home_shots': game_stats['shots'][0],
        'away_shots': game_stats['shots'][1],
        'home_penalties': game_stats['pim'][0],
        'away_penalties': game_stats['pim'][1],
        'goals': goals,
        'goalkeepers': goalkeepers,
        'extra': extra,
    }
    return output_text('messages/game_message.txt', to_template)


def player_stats(name_stats: str, table_name: str, column_name: str, count: int = 10) -> str:
    validate_table(table_name)
    validate_column(column_name)
    second_order = 'goals' if table_name == 'players_season_stats' else 'save_percentage'
    validate_column(second_order)

    q = sql.SQL(
        "SELECT r.lastname, pl.{col}, t.abbreviation AS team "
        "FROM {table} pl "
        "LEFT JOIN rosters r ON pl.player_id = r.player_id "
        "LEFT JOIN teams t ON t.team_id = r.current_team_id "
        "WHERE 1=1 "
        "ORDER BY pl.{col} DESC, pl.{second_order} DESC "
        "LIMIT %s"
    ).format(
        col=sql.Identifier(column_name),
        table=sql.Identifier(table_name),
        second_order=sql.Identifier(second_order),
    )
    stats = fetch_all(q, (count,), ['lastname', 'points', 'team'])

    to_template = {'name_stats': name_stats}
    players = []
    for i in range(stats['count_rows']):
        player_name = '%-16s' % stats['lastname'][i]
        stat_val = '%-4s' % stats['points'][i]
        team = stats['team'][i]
        players.append({'name': player_name, 'count': stat_val, 'team': team})

    to_template['players'] = players
    return output_text('messages/season_leaders_players.txt', to_template)


def team_table() -> str:
    stats = fetch_all(
        "SELECT short_name, games_played, points, procent_points, wins, "
        "       losses, ot, t.division_name, t.conference_name "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id "
        "ORDER BY conference_name, division_name, points DESC",
        columns=['short_name', 'games_played', 'points', 'procent_points',
                 'wins', 'losses', 'ot', 'division_name', 'conference_name'],
    )

    to_template = {'Atlantic': [], 'Metropolitan': [], 'Central': [], 'Pacific': []}
    for i in range(stats['count_rows']):
        name = '%-12s' % stats['short_name'][i]
        points = '%-4s' % stats['points'][i]
        games_played = '%-4s' % stats['games_played'][i]
        procent_points = '%-4s' % stats['procent_points'][i]
        to_template[stats['division_name'][i]].append({
            'short_name': name,
            'points': points,
            'games': games_played,
            'procent_points': procent_points,
        })

    return output_text('messages/league_table.txt', to_template)


def team_stats(name_stats: str, column_name: str) -> str:
    validate_column(column_name)

    q = sql.SQL(
        "SELECT short_name, {col}, games_played "
        "FROM teams_stats ts "
        "LEFT JOIN teams t ON ts.team_id = t.team_id "
        "ORDER BY {col} DESC, short_name"
    ).format(col=sql.Identifier(column_name))
    stats = fetch_all(q, columns=['team', 'points', 'games_played'])

    to_template = {'name_stats': name_stats}
    teams = []
    for i in range(stats['count_rows']):
        name = '%-14s' % stats['team'][i]
        stat_val = '%-5s' % stats['points'][i]
        gp = stats['games_played'][i]
        teams.append({'short_name': name, 'count': stat_val, 'games_played': gp})

    to_template['teams'] = teams
    return output_text('messages/team_stats.txt', to_template)


def day_digest(day=None) -> str:
    if day is None:
        latest_day = fetch_all(
            "SELECT max(day) AS day FROM games",
            columns=["day"],
        )
        day = latest_day["day"][0]
        if day is None:
            return "В базе пока нет завершенных матчей."
        day = str(day)

    game_ids = fetch_all(
        "SELECT DISTINCT game_id FROM games WHERE day = %s",
        (day,),
        ['game_id'],
    )
    if game_ids['count_rows'] == 0:
        return f'За {day} завершенных матчей не найдено.'

    message = ''
    for game_id in game_ids['game_id']:
        message = message + game_message(game_id)
        message += '\n\n'
    message = message.strip()
    if not message:
        return f'За {day} завершенных матчей не найдено.'
    return message
