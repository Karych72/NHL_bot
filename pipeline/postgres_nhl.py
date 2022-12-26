import psycopg2
import config


def get_pg_schema(table_name):
    query = f"""
    SELECT column_name, data_type
    FROM information_schema.columns
    where table_schema = 'dm' and table_name = '{table_name}';"""

    conn = psycopg2.connect(user=config.PG_USER,
                            # password=config.PG_PASSWORD,
                            host=config.PG_HOST,
                            port=config.PG_PORT,
                            database=config.PG_DATABASE)
    cursor = conn.cursor()
    cursor.execute(query)
    sql_schema = cursor.fetchall()
    schema = []
    for row in sql_schema:
        name = row[0]
        tp = row[1]
        schema.append({'name': name, 'type': tp})

    return schema


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
    # insert_values = list(insert_values)
    # is_open = False
    # for i in range(len(insert_values)):
    #     symb = insert_values[i]
    #     if symb == '{':
    #         is_open = True
    #     if symb == '}':
    #         is_open = False
    #     if symb == "'" and is_open:
    #         insert_values[i] = '"'
    # print(insert_values)
    # insert_values = ''.join(insert_values)
    # print(f'INSERT INTO {table_name} VALUES ' + insert_values[:-2])
    # cursor.execute(f'INSERT INTO {table_name} VALUES ' + insert_values[:-2])
    # print(f'INSERT INTO {table_name} VALUES ' + insert_values[:-2])
    # print()
    cursor.execute(f'INSERT INTO {table_name} VALUES ' + insert_values[:-2])
    # try:
    #     cursor.execute(f'INSERT INTO {table_name} VALUES ' + insert_values[:-2])
    # except:
    #     print(f'EXCEPTION! {table_name} is not loaded!')
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
