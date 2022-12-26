all_goals_potential_change_columns = ['goal_player_id', 'total_goals', 'assist_player1_id', 'assist_total_1',
                                      'assist_player2_id', 'assist_total_2', 'team_id']
all_goals_nan_values = [-9999, 0, -9999, 0, -9999, 0, -9999]
all_goalies_potential_change_columns = ['savePercentage', 'powerPlaySavePercentage', 'shortHandedSavePercentage',
                                        'evenStrengthSavePercentage']
all_goalies_nan_values = [100, 100, 100, 100]
all_players_potential_change_columns = ['faceOffPct']
all_players_nan_values = [0]

all_goals_columns = ['goal_player_id', 'total_goals', 'assist_player1_id', 'assist_total_1', 'assist_player2_id',
                     'assist_total_2', 'empty_net', 'winner_goal', 'is_ppg', 'is_shg', 'team_id',
                     'game_id', 'period', 'time', 'goals_away', 'goals_home']
game_team_stats_columns = ['goals', 'field', 'pim', 'shots', 'powerPlayPercentage', 'powerPlayGoals',
                           'powerPlayOpportunities', 'faceOffWinPercentage', 'blocked', 'takeaways', 'giveaways',
                           'hits', 'game_id', 'team_id', 'fst_period_goals', 'snd_period_goals', 'trd_period_goals']
game_player_stats_columns = ['team_id', 'game_id', 'player_id', 'timeOnIce', 'assists', 'goals', 'shots', 'hits',
                             'powerPlayGoals', 'powerPlayAssists', 'penaltyMinutes', 'faceOffWins', 'faceoffTaken',
                             'takeaways', 'giveaways', 'shortHandedGoals', 'shortHandedAssists', 'blocked',
                             'plusMinus', 'evenTimeOnIce', 'powerPlayTimeOnIce', 'shortHandedTimeOnIce',
                             'faceOffPct']

game_goalie_stats_columns = ['team_id', 'game_id', 'player_id', 'timeOnIce', 'assists', 'goals', 'pim', 'shots',
                             'saves', 'powerPlaySaves', 'shortHandedSaves', 'evenSaves', 'shortHandedShotsAgainst',
                             'evenShotsAgainst', 'powerPlayShotsAgainst', 'decision', 'savePercentage',
                             'powerPlaySavePercentage', 'shortHandedSavePercentage', 'evenStrengthSavePercentage']
game_stats_columns = ['game_id', 'day', 'home_team_id', 'away_team_id', 'winner_team_id', 'is_overtime',
                      'is_shootouts', 'season']
