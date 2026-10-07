import uvicorn
from app.config import get_setting

if __name__=="__main__":
    setting = get_setting()

    uvicorn.run(
        "app.api.main:app",
        host=setting.host,
        port=setting.port,
        reload=True,
        log_level=setting.log_level.lower()
    )