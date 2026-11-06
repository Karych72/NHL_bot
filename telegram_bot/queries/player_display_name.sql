-- Фамилия игрока для экранов бота: у однофамильцев сезона — с первой буквой имени
-- («A. Protas»), иначе одна фамилия. Сравнение по всей лиге, а не по матчу: один игрок
-- подписан одинаково на всех экранах. Полные тёзки по инициалу не различаются.
CREATE OR REPLACE FUNCTION player_display_name (
    full_name varchar, last_name varchar, season bigint
) RETURNS varchar
AS $$
    SELECT CASE
        WHEN (SELECT count(*) FROM rosters r
              WHERE r.season_id = season AND r.lastname = last_name) > 1
        THEN (left(full_name, 1) || '. ' || last_name)::varchar
        ELSE last_name
    END;
$$ LANGUAGE sql STABLE;
