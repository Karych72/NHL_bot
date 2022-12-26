import itertools
import argparse

import config
from functions import *
from columns import *
from postgres_nhl import *

DATAFRAMES_DIR = '../all_data/dataframes/myself_analyses'
JSON_DIR = '../all_data/dataframes/myself_analyses'

def to_int_column(df: pd.DataFrame, columns: list, nans_replace: list) -> pd.DataFrame:
    assert len(columns) == len(nans_replace)
    cnt = len(columns)
    for i in range(cnt):
        df[columns[i]] = df[columns[i]].fillna(nans_replace[i]).astype(int)
    return df


def to_double_column(df: pd.DataFrame, columns: list, nans_replace: list) -> pd.DataFrame:
    assert len(columns) == len(nans_replace)
    cnt = len(columns)
    for i in range(cnt):
        df[columns[i]] = df[columns[i]].fillna(nans_replace[i]).astype(float)
    return df


if __name__ == "__main__":
    start_date = config.DATE_FROM
    end_date = config.DATE_TO
    games_dict = get_games_df(start_date=start_date, end_date=end_date)

    listmerge = lambda lst: list(itertools.chain(*lst))

    all_goals = pd.DataFrame(listmerge([games_dict[now_id]['all_goals'] for now_id in games_dict.keys()]))
    game_team_stats_home = pd.DataFrame(
        listmerge([[games_dict[now_id]['game_team_stats_home']] for now_id in games_dict.keys()]))
    game_team_stats_away = pd.DataFrame(
        listmerge([[games_dict[now_id]['game_team_stats_away']] for now_id in games_dict.keys()]))
    game_player_stats_home = pd.DataFrame(
        listmerge([games_dict[now_id]['game_player_stats_home'] for now_id in games_dict.keys()]))
    game_goalie_stats_home = pd.DataFrame(
        listmerge([games_dict[now_id]['game_goalie_stats_home'] for now_id in games_dict.keys()]))
    game_player_stats_away = pd.DataFrame(
        listmerge([games_dict[now_id]['game_player_stats_away'] for now_id in games_dict.keys()]))
    game_goalie_stats_away = pd.DataFrame(
        listmerge([games_dict[now_id]['game_goalie_stats_away'] for now_id in games_dict.keys()]))

    game_stats = pd.DataFrame(listmerge([[games_dict[now_id]['game_stats']] for now_id in games_dict.keys()]))
    game_stats = game_stats[game_stats_columns]

    game_team_stats = pd.concat([game_team_stats_away, game_team_stats_home], ignore_index=True)
    game_team_stats = game_team_stats[game_team_stats_columns]

    all_goals = to_int_column(pd.DataFrame(all_goals), all_goals_potential_change_columns, all_goals_nan_values)
    all_goals = all_goals[all_goals_columns]

    game_player_stats = pd.concat([game_player_stats_home, game_player_stats_away], ignore_index=True)
    game_player_stats = to_double_column(game_player_stats, all_players_potential_change_columns,
                                         all_players_nan_values)
    game_player_stats = game_player_stats[game_player_stats_columns]

    game_goalie_stats = pd.concat([game_goalie_stats_home, game_goalie_stats_away], ignore_index=True)
    game_goalie_stats = to_double_column(game_goalie_stats, all_goalies_potential_change_columns,
                                         all_goalies_nan_values)
    game_goalie_stats.decision = game_goalie_stats.apply(lambda row: True if row['decision'] == 'W' else False, axis=1)
    game_goalie_stats = game_goalie_stats[game_goalie_stats_columns]

    suffix = str(start_date) + '_' + str(end_date)

    all_goals.to_csv(f'{DATAFRAMES_DIR}/all_goals_{suffix}.csv', sep=',', index=False)
    game_team_stats.to_csv(f'{DATAFRAMES_DIR}/game_team_stats_{suffix}.csv', sep=',', index=False)
    game_player_stats.to_csv(f'{DATAFRAMES_DIR}/game_player_stats_{suffix}.csv', sep=',', index=False)
    game_goalie_stats.to_csv(f'{DATAFRAMES_DIR}/game_goalie_stats_{suffix}.csv', sep=',', index=False)
    # print(game_stats)
    game_stats.to_csv(f'{DATAFRAMES_DIR}/games_{suffix}.csv', sep=',', index=False)

    write_json(game_and_goals_stats_to_json(all_goals), f'{JSON_DIR}/all_goals_{suffix}')
    write_json(game_and_goals_stats_to_json(game_stats), f'{JSON_DIR}/game_stats_{suffix}')

    write_json(player_team_stats_to_json(game_team_stats, 'field'), f'{JSON_DIR}/game_team_stats_{suffix}')
    write_json(player_team_stats_to_json(game_player_stats, 'player_id'), f'{JSON_DIR}/game_player_stats_{suffix}')
    write_json(player_team_stats_to_json(game_goalie_stats, 'player_id'), f'{JSON_DIR}/game_goalie_stats_{suffix}')

    print('data loading...')

    insert_pg('games', game_stats)
    print('game_stats has loaded!')

    delete_days('game_player_stats', config.DATE_FROM, config.DATE_TO)
    insert_pg('game_player_stats', game_player_stats)
    print('game_player_stats has loaded!')

    delete_days('game_team_stats', config.DATE_FROM, config.DATE_TO)
    insert_pg('game_team_stats', game_team_stats)
    print('game_team_stats has loaded!')

    delete_days('game_goalie_stats', config.DATE_FROM, config.DATE_TO)
    insert_pg('game_goalie_stats', game_goalie_stats)
    print('game_goalie_stats has loaded!')

    delete_days('all_goals', config.DATE_FROM, config.DATE_TO)
    insert_pg('all_goals', all_goals)
    print('all_goals has loaded!')

    print('all data has loaded!')



