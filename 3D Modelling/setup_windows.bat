@echo off
REM Setup script for 3D Geological Model Project on Windows

echo Creating virtual environment 'threeDimModel'...
python -m venv threeDimModel

echo.
echo Activating virtual environment...
call threeDimModel\Scripts\activate.bat

echo.
echo Upgrading pip...
python -m pip install --upgrade pip

echo.
echo Installing Python libraries from requirements.txt...
pip install -r requirements.txt

echo.
echo Installing Jupyter kernel for this environment...
python -m ipykernel install --user --name=threeDimModel --display-name "Python (threeDimModel)"

echo.
echo ========================================
echo Setup complete!
echo ========================================
echo.
echo To activate this environment in the future, run:
echo   threeDimModel\Scripts\activate.bat
echo.
echo To use in VS Code:
echo   1. Open Command Palette (Ctrl+Shift+P)
echo   2. Select "Python: Select Interpreter"
echo   3. Choose "Python (threeDimModel)"
echo.
echo To start Jupyter in VS Code:
echo   1. Open the .ipynb file
echo   2. Select the kernel "Python (threeDimModel)"
echo.
pause
