import uvicorn
from config import get_setting

if __name__=="__main__":
    setting = get_setting()

    uvicorn.run(
        "main:app",
        host=setting.host,
        port=setting.port,
        reload=True,
        log_level=setting.log_level.lower()
    )