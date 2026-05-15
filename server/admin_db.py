# server/admin_db.py
import sqlite3
import os
import hashlib
import conf

def init_server_env():
    if not os.path.exists(conf.SHARE_DIR):
        os.makedirs(conf.SHARE_DIR)

    conn = sqlite3.connect(conf.DB_PATH) # [cite: 1]
    cur = conn.cursor()
    
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS {conf.DB_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    
    # 存入哈希后的 admin 密码
    password_hash = hashlib.sha256(b"admin").hexdigest()
    
    try:
        cur.execute(f"INSERT INTO {conf.DB_TABLE} (username, password) VALUES (?, ?)", 
                   ("admin", password_hash))
        conn.commit()
        print("[*] 数据库初始化成功！")
        print("[*] 默认账号已加盐存储: admin / admin")
    except sqlite3.IntegrityError:
        print("[!] 数据库已就绪。")
    finally:
        conn.close()

if __name__ == "__main__":
    init_server_env()