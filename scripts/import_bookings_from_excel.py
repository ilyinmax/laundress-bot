import os
import sys
import hashlib
import pandas as pd
import psycopg2
from datetime import datetime

def gen_stub_tgid(surname: str, room: str) -> int:
    """
    Делаем отрицательный BIGINT из (Фамилия|Комната) — не пересечётся с реальными TG ID.
    """
    key = f"{(surname or '').strip().lower()}|{(room or '').strip().lower()}"
    h = int(hashlib.sha256(key.encode('utf-8')).hexdigest()[:15], 16)
    return - (h % 10**12)  # например: -123456789012

def main(xlsx_path: str):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    df = pd.read_excel(xlsx_path)

    # Ожидаемые колонки из твоего файла:
    # ['ID','Дата','Час','Машина','Тип','Фамилия','Комната']
    # Нормализуем дату и час
    df['date'] = pd.to_datetime(df['Дата']).dt.date
    df['hour'] = pd.to_datetime(df['Час']).dt.hour
    df['machine_name'] = df['Машина'].astype(str).str.strip()
    df['type'] = df['Тип'].astype(str).str.strip()
    df['surname'] = df['Фамилия'].astype(str).str.strip()
    df['room'] = df['Комната'].astype(str).str.strip()

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()

    # 1) Машины
    machines = df[['machine_name', 'type']].drop_duplicates()
    for _, r in machines.iterrows():
        cur.execute("""
            INSERT INTO machines (type, name)
            VALUES (%s, %s)
            ON CONFLICT (name) DO NOTHING
        """, (r['type'], r['machine_name']))

    # 2) Пользователи-заглушки (по Фамилия+Комната)
    users = df[['surname', 'room']].drop_duplicates()
    for _, r in users.iterrows():
        tg_id = gen_stub_tgid(r['surname'], r['room'])
        cur.execute("""
            INSERT INTO users (tg_id, surname, room)
            VALUES (%s, %s, %s)
            ON CONFLICT (tg_id) DO NOTHING
        """, (tg_id, r['surname'], r['room']))

    # 3) Бронирования
    inserted = 0
    skipped = 0
    for _, r in df.iterrows():
        # находим ids
        cur.execute("SELECT id FROM machines WHERE name=%s AND type=%s", (r['machine_name'], r['type']))
        mrow = cur.fetchone()
        if not mrow:
            print("WARN: machine not found:", r['machine_name'], r['type'])
            skipped += 1
            continue
        machine_id = mrow[0]

        tg_id = gen_stub_tgid(r['surname'], r['room'])
        cur.execute("SELECT id FROM users WHERE tg_id=%s", (tg_id,))
        urow = cur.fetchone()
        if not urow:
            print("WARN: user not found:", r['surname'], r['room'])
            skipped += 1
            continue
        user_id = urow[0]

        try:
            cur.execute("""
                INSERT INTO bookings (user_id, machine_id, date, hour)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (machine_id, date, hour) DO NOTHING
            """, (user_id, machine_id, r['date'], int(r['hour'])))
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print("ERROR row:", e)
            skipped += 1

    print(f"Done. Inserted: {inserted}, skipped/duplicates: {skipped}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_bookings_from_excel.py <path_to_xlsx>")
        sys.exit(1)
    main(sys.argv[1])
