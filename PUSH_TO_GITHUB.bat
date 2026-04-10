@echo off
echo ============================================
echo  Webpage to PDF Converter - Push to GitHub
echo ============================================
echo.
echo Step 1: Go to https://github.com/new
echo         Create a NEW repo named: pdf-converter-web
echo         Set it to PUBLIC, no README
echo         Click "Create repository"
echo.
pause

cd /d "%~dp0"
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/abhiravgotra92/pdf-converter-web.git
git push -u origin main

echo.
echo ============================================
echo  Done! Now deploy on Railway:
echo  1. Go to https://railway.app
echo  2. New Project > Deploy from GitHub
echo  3. Select: pdf-converter-web
echo  4. Add env var: APP_PASSWORD = artest
echo  5. Live in ~3 minutes!
echo ============================================
pause
