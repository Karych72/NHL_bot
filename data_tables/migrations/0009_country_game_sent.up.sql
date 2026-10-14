-- Миграция 0009: подписка на страну шлётся по матчу (Задача 76) — сообщение уходит на первом
-- прогоне sync после конца матча, а не одной рассылкой за ночь. Отметка «матч отправлен»
-- живёт здесь, по строке подписки; last_sent_night у country_players больше не пишется.

CREATE TABLE bot_country_game_sent (
    subscription_id BIGINT NOT NULL REFERENCES bot_subscriptions (id) ON DELETE CASCADE,
    game_id BIGINT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subscription_id, game_id)
);

-- Ночь, уже разосланную прежним кодом (last_sent_night), — отметить по её матчам: иначе
-- первый прогон после деплоя прислал бы эту ночь второй раз.
INSERT INTO bot_country_game_sent (subscription_id, game_id)
SELECT s.id, g.game_id
FROM bot_subscriptions s
JOIN games g ON g.day = s.last_sent_night
WHERE s.kind = 'country_players' AND s.last_sent_night IS NOT NULL;
