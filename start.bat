@echo off
chcp 65001 >nul
echo ========================================
echo   短视频无水印解析 - 本地服务
echo ========================================
echo.

:: 检查Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到Python，请先安装Python 3.9+
    echo 下载: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: 安装依赖
echo [1/2] 安装依赖...
pip install -r requirements.txt -q

:: 启动服务
echo [2/2] 启动服务...
echo.
echo  访问地址: http://localhost:8848
echo  API文档:  http://localhost:8848/docs
echo  按 Ctrl+C 停止服务
echo.
python server.py
pause
