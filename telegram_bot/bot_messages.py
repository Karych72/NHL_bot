import config
from sql_query import generate_query, query
from template_funcs import output_text


def get_nationality(player_name):
    pass


def game_message(game_id: int) -> str:

    game_query_stats = f'select * from get_game_stats({game_id})'
    game_query_goals = f'select * from get_goals_game({game_id})'
    game_query_goalies = f'select * from get_goalies_game({game_id})'

    game_stats = query(game_query_stats, ['goals', 'pim', 'blocks', 'hits', 'shots', 'is_overtime',
                                          'is_shootout', 'field', 'team_name'])
    # print(game_stats)

    game_goals = query(game_query_goals, ['scorer', 'assist_1', 'assist_2', 'period', 'time', 'home_score', 'away_score'])

    game_goalies = query(game_query_goalies, ['shots', 'saves', 'timeonice', 'lastname',
                                              'save_percentage', 'is_home'])
    print(game_goalies)

    if not game_stats['is_overtime'][0]:
        extra = ''
    elif not game_stats['is_shootout'][0]:
        extra = '(OT)'
    else:
        extra = '(Б)'

    goals = []
    for i in range(game_goals['count_rows']):
        scorer = game_goals['scorer'][i]
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
        goalkeepers += f"{game_goalies['lastname'][i]} " \
                       f"({game_goalies['saves'][i]}/{game_goalies['shots'][i]}, " \
                       f"{round(game_goalies['save_percentage'][i], 2)}%, {game_goalies['timeonice'][i]})"
    to_template = {'team_home': game_stats['team_name'][0],
                   'team_away': game_stats['team_name'][1],

                   'home_score': game_stats['goals'][0],
                   'away_score': game_stats['goals'][1],

                   'home_shots': game_stats['shots'][0],
                   'away_shots': game_stats['shots'][1],

                   'home_penalties': game_stats['pim'][0],
                   'away_penalties': game_stats['pim'][1],

                   'goals': goals,
                   'goalkeepers': goalkeepers,
                   'extra': extra
                   }
    return output_text('messages/game_message.txt', to_template)


# print(game_message(2022020434))


def player_stats(name_stats: str, table_name: str, column_name: str, count=10, condition='') -> str:
    if table_name == 'players_season_stats':
        second_order = 'goals'
    else:
        second_order = 'save_percentage'
    stats = query(f"""  select r.lastname, pl.{column_name}, t.abbreviation as team
                        from {table_name} pl
                        left join rosters r
                        on pl.player_id = r.player_id
                        left join teams t
                        on t.team_id = r.current_team_id
                        where 1=1 {condition}
                        order by {column_name} desc, {second_order} desc
                        limit {count};""", ['lastname', 'points', 'team']
                  )
    # print(stats)
    to_template = {'name_stats': name_stats}
    players = []
    for i in range(stats['count_rows']):
        name = '%-12s' % (stats['lastname'][i])
        count = '%-4s' % (stats['points'][i])
        team = stats['team'][i]
        players.append({'name': name,
                        'count': count,
                        'team': team
                        })

    to_template['players'] = players
    # print(to_template)
    return output_text('messages/season_leaders_players.txt', to_template)


def team_table():
    stats = query(f"""  select short_name, games_played, points, procent_points, wins, 
                               losses, ot, t.division_name, t.conference_name
                        from teams_stats ts
                        left join teams t
                        on ts.team_id = t.team_id
                        order by conference_name, division_name, points desc""",

                  ['short_name', 'games_played', 'points', 'procent_points', 'wins',
                   'losses', 'ot', 'division_name', 'conference_name'])

    to_template = {'Atlantic': [], 'Metropolitan': [], 'Central': [], 'Pacific': []}
    for i in range(stats['count_rows']):
        name = '%-12s' % (stats['short_name'][i])
        points = '%-4s' % (stats['points'][i])
        games_played = '%-4s' % (stats['games_played'][i])
        procent_points = '%-4s' % (stats['procent_points'][i])
        to_template[stats['division_name'][i]].append({'short_name': name,
                                                       'points': points,
                                                       'games': games_played,
                                                       'procent_points': procent_points
                                                       })

    # print(to_template)
    return output_text('messages/league_table.txt', to_template)


print(team_table())


def team_stats():
    pass


def gay_digest():
    pass
