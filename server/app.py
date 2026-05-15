# server/app.py
from wsgidav.wsgidav_app import WsgiDAVApp
from auth import SQLiteDomainController

def create_dav_app(main_config, db_path):
    dav_config = main_config.copy()
    
    # 核心修正 1：类属性注入，确保所有实例都能拿到路径
    SQLiteDomainController.db_path = db_path
    
    dav_config.update({
        "http_authenticator": {
            "domain_controller": SQLiteDomainController,
            # 核心修正 2：严格遵守 WsgiDAV 的安全策略
            "accept_basic": True,
            "accept_digest": False,      # 必须为 False
            "default_to_digest": False,  # 报错提示必须加这一行
        },
        "dir_browser": {"enable": False},
    })
    return WsgiDAVApp(dav_config)