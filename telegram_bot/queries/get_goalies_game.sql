CREATE OR REPLACE FUNCTION get_goalies_game (now_game_id int)
    RETURNS TABLE(shots             int, --total_score int,
                  saves             int, --total_assist_1 int,
                  timeonice         varchar(20),
                  lastname          varchar(50),
                  save_percentage   DOUBLE PRECISION,
                  is_home           boolean)
AS $$
BEGIN
    RETURN QUERY select distinct ggs.shots, ggs.saves, ggs.timeonice,
                 r.lastname, ggs.save_percentage,
                 g.home_team_id = ggs.team_id as is_home
                 from game_goalie_stats ggs
                 left join rosters r
                 on ggs.player_id = r.player_id
                 left join games g
                 on g.game_id = ggs.game_id
                 where ggs.game_id = now_game_id
                 order by is_home desc
                 ;
END; $$

LANGUAGE 'plpgsql';