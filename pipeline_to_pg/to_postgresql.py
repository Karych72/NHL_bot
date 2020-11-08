import requests
import itertools
import numpy as np
from functools import reduce
from operator import add
import pandas as pd
import argparse
from functions import *
from columns import *
import psycopg2 as ps


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
    parser = argparse.ArgumentParser()
    parser.add_argument('start_date', type=str)
    parser.add_argument('end_date', type=str)

    args = parser.parse_args()
    games_dict = get_games_df(start_date=args.start_date, end_date=args.end_date)

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

    game_team_stats = pd.concat([game_team_stats_away, game_team_stats_home])
    game_team_stats = game_team_stats[game_team_stats_columns]

    all_goals = to_int_column(pd.DataFrame(all_goals), all_goals_potentional_change_columns, all_goals_nan_values)
    all_goals = all_goals[all_goals_columns]

    game_player_stats = pd.concat([game_player_stats_home, game_player_stats_away])
    game_player_stats = to_double_column(game_player_stats, all_players_potentional_change_columns,
                                         all_players_nan_values)
    game_player_stats = game_player_stats[game_player_stats_columns]

    game_goalie_stats = pd.concat([game_goalie_stats_home, game_goalie_stats_away])
    game_goalie_stats = to_double_column(game_goalie_stats, all_goalies_potentional_change_columns,
                                         all_goalies_nan_values)
    game_goalie_stats.decision = game_goalie_stats.apply(lambda row: True if row['decision'] == 'W' else False, axis=1)
    game_goalie_stats = game_goalie_stats[game_goalie_stats_columns]

    all_goals.to_csv('all_goals.csv', sep=',', index=False)
    game_team_stats.to_csv('game_team_stats.csv', sep=',', index=False)
    game_player_stats.to_csv('game_player_stats.csv', sep=',', index=False)
    game_goalie_stats.to_csv('game_goalie_stats.csv', sep=',', index=False)
    game_stats.to_csv('games.csv', sep=',', index=False)

    conn = ps.connect("""
        host=host
        port=port
        dbname=nhl
        user=user
        password=password
        target_session_attrs=read-write
        sslmode=verify-full
    """)

    cur = conn.cursor()
    table_name = 'all_goals'
    with open(table_name + '.csv', 'r') as f:
        next(f)
        cur.copy_from(f, table_name, sep=',')

    table_name = 'game_team_stats'
    with open(table_name + '.csv', 'r') as f:
        next(f)
        cur.copy_from(f, table_name, sep=',')

    table_name = 'game_player_stats'
    with open(table_name + '.csv', 'r') as f:
        next(f)
        cur.copy_from(f, table_name, sep=',')

    table_name = 'game_goalie_stats'
    with open(table_name + '.csv', 'r') as f:
        next(f)
        cur.copy_from(f, table_name, sep=',')

    table_name = 'games'
    with open(table_name + '.csv', 'r') as f:
        next(f)
        cur.copy_from(f, table_name, sep=',')

    cur.close()
    conn.commit()
