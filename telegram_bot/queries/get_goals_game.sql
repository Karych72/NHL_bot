DROP FUNCTION IF EXISTS get_goals_game(int);
CREATE OR REPLACE FUNCTION get_goals_game (now_game_id int)
    RETURNS TABLE(scorer varchar(50),
                  scorer_position varchar(5),
                  assist_1 varchar(50),
                  assist_2 varchar(50),
                  period int,
                  goal_time varchar(20),
                  home_score int,
                  away_score int,
                  is_ppg boolean,
                  is_shg boolean,
                  empty_net boolean,
                  winner_goal boolean,
                  goal_game_id bigint,
                  goal_event_id int)
AS $$
BEGIN
    RETURN QUERY SELECT
               gs.lastname as scorer,
               gs.position as scorer_position,
	           a1.lastname as assist_1,
	           a2.lastname as assist_2,
	           g.period,
	           g.time as goal_time,
	           goals_home as home_score,
	           goals_away as away_score,
	           g.is_ppg,
	           g.is_shg,
	           g.empty_net,
	           g.winner_goal,
	           g.game_id as goal_game_id,
	           g.event_id as goal_event_id
        from all_goals as g
        left join games as gm on gm.game_id = g.game_id
        left join rosters as gs
        on g.goal_player_id = gs.player_id and gs.season_id = gm.season_id
        left join rosters as a1
        on g.assist_player1_id = a1.player_id and a1.season_id = gm.season_id
        left join rosters as a2
        on g.assist_player2_id = a2.player_id and a2.season_id = gm.season_id
        where g.game_id = now_game_id
        order by period, time ;
END; $$

LANGUAGE 'plpgsql';