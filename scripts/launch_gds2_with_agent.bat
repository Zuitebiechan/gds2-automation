@echo off
REM ============================================================
REM  Launch GDS2 with Java Agent for RPA automation
REM
REM  This is a copy of the original LaunchGDS2.bat with the
REM  -javaagent flag added so Python can communicate with GDS2
REM  via ~/gds2-data/command.json and result.json.
REM ============================================================

cd /d "%~dp0"
cd /d "C:\Program Files (x86)\GDS 2\bin"

set AGENT_JAR=C:\tools\gds2-agent\gds2-agent.jar

if not exist "%AGENT_JAR%" (
    echo [ERROR] Agent JAR not found: %AGENT_JAR%
    echo Please ensure gds2-agent.jar is at the expected location.
    pause
    exit /b 1
)

echo Starting GDS2 with Java Agent...
echo Agent JAR: %AGENT_JAR%
echo.

"..\jre6\bin\java.exe" ^
    -Xms250m -Xmx768m ^
    -Dprism.order=sw ^
    -javaagent:"%AGENT_JAR%" ^
    -cp RTKApplet.jar;.\iText-5.0.2.jar;"..\..\GM\TIS2WebProxy\dls-nativelibs.jar";"..\..\GM\TIS2WebProxy\t2w-proxy.jar";"..\..\GM\TIS2WebProxy\t2w-proxy-impl.jar";"..\..\GM\TIS2WebProxy\jCookie-0.8c.jar";"..\..\GM\TIS2WebProxy\jRegistryKey.jar";"..\..\GM\TIS2WebProxy\log4j-1.2.15.jar";"..\..\GM\TIS2WebProxy\scsm.jar";TeeChart.Swing.jar;TeeChart.SWT.jar; ^
    com.Mahle.Applets.RXMainFX
