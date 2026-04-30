#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import signal
import sqlite3
from cheroot import wsgi
from wsgidav.wsgidav_app import WsgiDAVApp
from wsgidav.dc.base_dc import BaseDomainController

from server.conf import *
import server.admin_db as admin_db 

class SQLiteDomainController(BaseDomainController):
    def __init__(self, wsgidav_app, config):
        super().__init__(wsgidav_app, config)

    def get_domain_realm(self, path_info, environ):
        return "WebDAV Storage"

    def require_authentication(self, realm, path_info):
        return True

    def supports_http_digest_auth(self):
        return False

    def basic_auth_user(self, realm, user_name, password, environ):
        try:
            # 这里的 DB_PATH 是从 config 读过来的
            conn = sqlite3.connect(config.DB_PATH)
            cur = conn.cursor()
            # Selct语句
            cur.execute("SELECT password FROM dav_users WHERE username = ?", (user_name,))
            row = cur.fetchone()
            conn.close()

            if row:
                return user_name, row[0]
            return None
        except Exception as e:
            print(f"Auth DB error: {e}")
            return None

    # check_credentials 可以不重写，基类会使用上面返回的密码自动比对

config["http_authenticator"] ={
        "domain_controller": SQLiteDomainController,
        "accept_basic": True,
        "accept_digest": False,
        "default_to_digest": False,
    },
config["dir_browser"] = {"enable": False}


# 修改后的启动入口
if __name__ == "__main__":
    # 核心改进：启动前先自检
    print("--- Zotero WebDAV Server Self-Check ---")
    admin_db.init_server_env()
    
    app = WsgiDAVApp(config.WSGIDAV_CONFIG) # 将 WsgiDAV 的配置也挪到 config 里
    server = wsgi.Server(
        bind_addr=(config.HOST, config.PORT),
        wsgi_app=app,
    )

    # ... 信号处理逻辑保持不变 ...

    print(f"[*] Server is running on http://{config.HOST}:{config.PORT}")
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()