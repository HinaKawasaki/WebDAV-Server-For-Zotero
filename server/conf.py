# DataBase Conf
DB_HOST = "127.0.0.1"
DB_PORT = 3306
DB_USER = "webdav_server"
DB_PASSWORD = "webdav"
DB_NAME = "webdav"
DB_TABLE = "dav_users"
SHARE_DIR = "./zotero"
# WebDav Server Conf
config = {
    "host": "0.0.0.0",
    "port": 8000,
    "verbose": 3,
    "provider_mapping": {
        "/": SHARE_DIR,
    },
}