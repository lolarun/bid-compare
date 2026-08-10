import sqlite3
conn = sqlite3.connect('data/mempas.db')
cur = conn.cursor()
cur.execute(
    "SELECT id, filename, file_path FROM extraction_jobs WHERE id IN (?, ?)",
    ('490dcd878d7e4113b60ae9defab93f82', '8ba09636f9c94a47a60b1af8252a9a09')
)
for row in cur.fetchall():
    print(row)
conn.close()
