CREATE OR REPLACE FUNCTION get_game_stats (now_game_id int)
    RETURNS TABLE(goals         int,
                  pim           int,
                  blocked       int,
                  hits          int,
                  is_overtime   boolean,
                  is_shootouts  boolean,
                  field         varchar(20)
       )
AS $$
BEGIN
    RETURN QUERY SELECT distinct gs.goals, gs.pim, gs.blocked, gs.hits,
                                 g.is_overtime, g.is_shootouts, gs.field
                 from game_team_stats gs
                 left join games g
                 on gs.game_id = g.game_id
                 where gs.game_id = now_game_id
                 order by field desc;
END; $$

LANGUAGE 'plpgsql';