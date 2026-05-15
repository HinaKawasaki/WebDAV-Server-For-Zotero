# start_server.py
from cheroot import wsgi
from conf import config, DB_PATH
from app import create_dav_app
import admin_db as admin_db

if __name__ == "__main__":
    # 1. 环境初始化
    admin_db.init_server_env()
    
    # 2. 组装应用
    app = create_dav_app(config, DB_PATH)
    
    # 3. 启动服务器
    server = wsgi.Server(
        bind_addr=(config["host"], config["port"]),
        wsgi_app=app,
    )
    
    print(f"[*] Server running on {config['host']}:{config['port']}")
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()