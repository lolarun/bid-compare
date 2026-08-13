import sqlite3
conn = sqlite3.connect("data/mempas.db")
conn.execute("ALTER TABLE quotes ADD COLUMN bid_status VARCHAR(20) DEFAULT ''")
conn.commit()
print("Column bid_status added to quotes table")
conn.close()
