import psycopg2
from typing import List

import config


def generate_query(general_script, where=None, order_by=None, limit=None):
    result_script = general_script
    if where is not None:
        result_script = result_script + 'and ' + where
    if order_by is not None:
        result_script = result_script + ' order by ' + order_by
    if limit is not None:
        result_script = result_script + ' limit ' + str(limit)
    return result_script


def query(script: str, columns: List[str]) -> dict:
    conn = psycopg2.connect(user=config.PG_USER,
                            # password=config.PG_PASSWORD,
                            host=config.PG_HOST,
                            port=config.PG_PORT,
                            database=config.PG_DATABASE)
    cursor = conn.cursor()

    cursor.execute(script)

    output_query = cursor.fetchall()
    to_return = {column: [] for column in columns}
    to_return['count_rows'] = len(output_query)
    for row in output_query:
        for i in range(len(columns)):
            to_return[columns[i]].append(row[i])
    return to_return


example_query = f'''select lastname, wins, save_percentage, games, shutouts
from goalies_season_stats pl
left join rosters r
on pl.player_id = r.player_id
left join teams t
on t.team_id = r.current_team_id
where 1=1'''


# print(query(generate_query(example_query, where='wins < 15 and shutouts > 0', order_by='wins desc', limit=10), ['lastname', 'wins', 'save_percentage', 'games', 'shutouts']))
