import uvicorn
from app.config import get_setting
import os
import sys
app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
sys.path.insert(0, app_dir)

if __name__=="__main__":
    setting = get_setting()

    uvicorn.run(
        "app.main:app",
        host=setting.host,
        port=setting.port,
        reload=True,
        log_level=setting.log_level.lower()
    )