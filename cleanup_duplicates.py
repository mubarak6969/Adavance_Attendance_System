"""One-time maintenance utility for databases created before the unique
(student_id, date) index existed on the attendance table.

New installs and app.py's own startup (via db.init_db) already prevent and
self-heal duplicate same-day attendance rows, so this script is normally
unnecessary -- keep it only for migrating an older exported database.

Usage:
    python cleanup_duplicates.py [path/to/attendance.db]
"""
import sys
import sqlite3

from config import get_config


def cleanup_duplicates(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, student_id, date(timestamp) AS d FROM attendance ORDER BY timestamp ASC"
        ).fetchall()

        seen = set()
        to_delete = []
        for row in rows:
            key = (row["student_id"], row["d"])
            if key in seen:
                to_delete.append(row["id"])
            else:
                seen.add(key)

        if to_delete:
            conn.executemany("DELETE FROM attendance WHERE id=?", [(i,) for i in to_delete])
            conn.commit()

        print(f"Deleted {len(to_delete)} duplicate attendance record(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else get_config().DATABASE_PATH
    cleanup_duplicates(target)
