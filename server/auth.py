# server/auth.py
from wsgidav.dc.base_dc import BaseDomainController
import sqlite3
import hashlib

class SQLiteDomainController(BaseDomainController):
    # 类变量，由 app.py 在启动前注入
    db_path = None

    def __init__(self, wsgidav_app, config):
        # 严格遵守基类的两个参数要求
        super().__init__(wsgidav_app, config)

    def get_domain_realm(self, path_info, environ):
        return "Zotero WebDAV Storage"

    def require_authentication(self, realm, path_info):
        return True

    def supports_http_digest_auth(self):
        return False

    def basic_auth_user(self, realm, user_name, password, environ):
        # 这里的 hashlib 已经可以防御“明文泄露”了
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT password FROM dav_users WHERE username = ?", (user_name,))
                row = cur.fetchone()
                if row and row[0] == password_hash:
                    return user_name
            return None
        except Exception as e:
            print(f"Auth Error: {e}")
            return None