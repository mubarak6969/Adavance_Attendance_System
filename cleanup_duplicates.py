import sqlite3
import datetime

DB_PATH = "attendance.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Get all records
c.execute("SELECT id, student_id, date(timestamp) FROM attendance ORDER BY timestamp ASC")
rows = c.fetchall()

# Track first record per student per day
seen = set()
to_delete = []

for row in rows:
    key = (row[1], row[2])  # (student_id, date)
    if key in seen:
        to_delete.append(row[0])  # store duplicate id
    else:
        seen.add(key)

# Delete duplicates
for record_id in to_delete:
    c.execute("DELETE FROM attendance WHERE id=?", (record_id,))

conn.commit()
conn.close()
print(f"Deleted {len(to_delete)} duplicate records.")
