import sqlite3
import os
import server.conf as conf  # 引用剥离出来的 config.py

def init_server_env():
    """初始化服务端环境：创建存储目录和数据库"""
    # 1. 创建数据存储目录
    if not os.path.exists(conf.SHARE_DIR):
        os.makedirs(conf.SHARE_DIR)
        print(f"[*] 已创建存储目录: {conf.SHARE_DIR}")

    # 2. 初始化 SQLite 数据库[cite: 1]
    conn = sqlite3.connect(conf.DB_PATH)
    cur = conn.cursor()
    
    # 建表逻辑
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS {conf.DB_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    
    # 3. 自动创建一个“快速启动”账户
    # 既然有 SSH 隧道保护，这里用 admin/admin 方便演示
    try:
        cur.execute(f"INSERT INTO {conf.DB_TABLE} (username, password) VALUES (?, ?)", 
                   ("admin", "admin"))
        conn.commit()
        print("[*] 数据库初始化成功！")
        print("[*] 默认演示账号: 用户名=admin, 密码=admin")
    except sqlite3.IntegrityError:
        print("[!] 数据库已就绪，无需重复初始化。")
    finally:
        conn.close()

if __name__ == "__main__":
    init_server_env()