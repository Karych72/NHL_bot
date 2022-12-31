CREATE OR REPLACE FUNCTION get_goals_game (now_game_id int)
    RETURNS TABLE(scorer varchar(50), --total_score int,
                  assist_1 varchar(50), --total_assist_1 int,
                  assist_2 varchar(50), --total_assist_2 int,
                  home_score int,
                  away_score int)
AS $$
BEGIN
    RETURN QUERY SELECT
               gs.lastname as scorer,
	           a1.lastname as assist_1,
	           a2.lastname as assist_2,
	           goals_home as home_score,
	           goals_away as away_score
        from all_goals as g
        left join rosters as gs
        on g.goal_player_id = gs.player_id
        left join rosters as a1
        on g.assist_player1_id = a1.player_id
        left join rosters as a2
        on g.assist_player2_id = a2.player_id
        where g.game_id = now_game_id
        order by period, time ;
END; $$

LANGUAGE 'plpgsql';