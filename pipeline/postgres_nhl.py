import psycopg2
import config


def delete_days(table_name, date_from, date_to) -> None:
    conn = psycopg2.connect(user=config.PG_USER,
                            # password=config.PG_PASSWORD,
                            host=config.PG_HOST,
                            port=config.PG_PORT,
                            database=config.PG_DATABASE)
    cursor = conn.cursor()

    cursor.execute(f'''
    delete from {table_name} t
    where exists (
         select game_id 
         from games g
         where g.day >= '{date_from}' and g.day <= '{date_to}'
           and t.game_id = g.game_id)
    ''')

    conn.commit()


def insert_pg(table_name, df) -> None:
    conn = psycopg2.connect(user=config.PG_USER,
                            # password=config.PG_PASSWORD,
                            host=config.PG_HOST,
                            port=config.PG_PORT,
                            database=config.PG_DATABASE)
    cursor = conn.cursor()
    insert_values = ''
    values = df.values.tolist()

    for value in values:
        now_row = '(' + ','.join("'" + str(row).replace("'", "") + "'" if str(row) not in ['', "['']"] else 'null'
                                 for row in list(value)) + '), '
        insert_values = insert_values + now_row

    # print(f'INSERT INTO {table_name} VALUES ' + insert_values[:-2])
    try:
        cursor.execute(f'INSERT INTO {table_name} VALUES ' + insert_values[:-2])
    except:
        print(f'EXCEPTION! {table_name} is not loaded!')
    conn.commit()

    return


def truncate_table(table_name) -> None:
    conn = psycopg2.connect(user=config.PG_USER,
                            # password=config.PG_PASSWORD,
                            host=config.PG_HOST,
                            port=config.PG_PORT,
                            database=config.PG_DATABASE)
    cursor = conn.cursor()

    cursor.execute(f'truncate table {table_name}')
    conn.commit()

    return
