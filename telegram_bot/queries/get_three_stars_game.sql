CREATE OR REPLACE FUNCTION get_three_stars_game (now_game_id bigint)
    RETURNS TABLE(star              int,
                  lastname          varchar(50),
                  player_position   varchar(5),
                  abbreviation      varchar(10),
                  goals             int,
                  assists           int,
                  saves             int,
                  shots             int,
                  save_percentage   double precision)
AS $$
BEGIN
    RETURN QUERY select ts.star, r.lastname, r.position as player_position, t.abbreviation,
                 gps.goals, gps.assists, ggs.saves, ggs.shots, ggs.save_percentage
                 from game_three_stars ts
                 left join games g
                 on g.game_id = ts.game_id
                 left join rosters r
                 on ts.player_id = r.player_id and r.season_id = g.season_id
                 left join teams t
                 on ts.team_id = t.team_id and t.season_id = g.season_id
                 left join game_player_stats gps
                 on gps.game_id = ts.game_id and gps.player_id = ts.player_id
                 left join game_goalie_stats ggs
                 on ggs.game_id = ts.game_id and ggs.player_id = ts.player_id
                 where ts.game_id = now_game_id
                 order by ts.star
                 ;
END; $$

LANGUAGE 'plpgsql';
