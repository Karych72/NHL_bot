CREATE OR REPLACE FUNCTION get_game_stats (now_game_id int)
    RETURNS TABLE(goals             int,
                  pim               int,
                  blocked           int,
                  hits              int,
                  shots             int,
                  is_overtime       boolean,
                  is_shootouts      boolean,
                  field             varchar(20),
                  team_name         varchar(20)
       )
AS $$
BEGIN
    RETURN QUERY SELECT distinct gs.goals, gs.pim, gs.blocked, gs.hits, gs.shots, g.is_overtime,
                                 g.is_shootouts, gs.field, t.short_name as team_name
                 from game_team_stats gs
                 left join games g
                 on gs.game_id = g.game_id
                 left join teams t
                 on t.team_id = gs.team_id
                 where gs.game_id = now_game_id
                 order by field desc;
END;
$$

LANGUAGE 'plpgsql';