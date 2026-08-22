from pathlib import Path

from dotenv import load_dotenv
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)
nonebot.load_plugin("plugins.violation_record")
nonebot.load_plugin("plugins.feature_control")
nonebot.load_plugin("plugins.feature_control.matcher")
nonebot.load_plugin("plugins.chat_archive")
nonebot.load_plugin("plugins.member_memory.matcher")
nonebot.load_plugin("plugins.random_chat")
nonebot.load_plugin("plugins.chat_vision")
nonebot.load_plugin("plugins.private_memory")
nonebot.load_plugin("plugins.group_router")
nonebot.load_plugin("plugins.private_chat")

if __name__ == "__main__":
    nonebot.run()
