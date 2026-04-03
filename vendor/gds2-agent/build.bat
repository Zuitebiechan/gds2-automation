@echo off
REM Build GDS2 Agent JAR
REM
REM Prerequisites:
REM   - JDK 11+ (javac, jar commands available)
REM   - JavaFX SDK on module path OR use the GDS2 runtime JRE
REM
REM Usage:
REM   build.bat
REM
REM Output:
REM   build\gds2-agent.jar
REM
REM To deploy:
REM   copy build\gds2-agent.jar C:\tools\gds2-agent\gds2-agent.jar

setlocal EnableDelayedExpansion

set SCRIPT_DIR=%~dp0
set SRC_DIR=%SCRIPT_DIR%src\main\java
set BUILD_DIR=%SCRIPT_DIR%build
set CLASSES_DIR=%BUILD_DIR%\classes
set JAR_FILE=%BUILD_DIR%\gds2-agent.jar

REM Original JAR for classpath (contains Gson + existing classes)
set ORIGINAL_JAR=C:\tools\gds2-agent\gds2-agent.jar

REM JavaFX modules path (GDS2 ships its own JRE with JavaFX)
REM Adjust if needed for your environment
set "GDS2_HOME=C:\GM\GDS2\GDS2"
set "GDS2_HOME_X86=C:\Program Files (x86)\GDS 2"

echo === Building GDS2 Agent ===
echo Source: %SRC_DIR%
echo Output: %JAR_FILE%
echo.

REM Clean
if exist "%CLASSES_DIR%" rmdir /s /q "%CLASSES_DIR%"
mkdir "%CLASSES_DIR%"

REM Find JavaFX jars for compilation
set "JAVAFX_JAR="
if exist "%GDS2_HOME_X86%\jre6\lib\ext\jfxrt.jar" (
    echo Using GDS2 x86 JRE6 for JavaFX classes
    set "JAVAFX_JAR=%GDS2_HOME_X86%\jre6\lib\ext\jfxrt.jar"
)
if not defined JAVAFX_JAR if exist "%GDS2_HOME%\jre\lib\jfxrt.jar" (
    echo Using GDS2 JRE for JavaFX classes
    set "JAVAFX_JAR=%GDS2_HOME%\jre\lib\jfxrt.jar"
)

if not defined JAVAFX_JAR (
    echo.
    echo Could not locate jfxrt.jar for compilation.
    echo Checked:
    echo   !GDS2_HOME_X86!\jre6\lib\ext\jfxrt.jar
    echo   !GDS2_HOME!\jre\lib\jfxrt.jar
    echo.
    exit /b 1
)

REM Build classpath
set "CP=%ORIGINAL_JAR%"
if defined JAVAFX_JAR set "CP=%CP%;%JAVAFX_JAR%"

echo Classpath: %CP%
echo.

REM Extract original JAR classes (we need all existing classes)
echo Extracting original JAR...
pushd "%CLASSES_DIR%"
jar xf "%ORIGINAL_JAR%"
popd

REM Compile only the new/modified files so they overwrite extracted classes
echo Compiling patched classes...
javac -cp "%CP%" ^
      -d "%CLASSES_DIR%" ^
      -source 8 -target 8 ^
      "%SRC_DIR%\com\gds2\agent\PageIdentifier.java" ^
      "%SRC_DIR%\com\gds2\agent\CommandMonitor.java"

if errorlevel 1 (
    echo.
    echo === BUILD FAILED ===
    exit /b 1
)

echo Compilation successful.
echo.

REM Build new JAR with manifest
echo Building JAR...
if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"
if exist "%JAR_FILE%" del /f /q "%JAR_FILE%"

REM Copy manifest from original
jar cf "%JAR_FILE%" -C "%CLASSES_DIR%" .

REM Update manifest to keep Premain-Class
echo Premain-Class: com.gds2.agent.GDS2Agent > "%BUILD_DIR%\manifest-addition.txt"
jar ufm "%JAR_FILE%" "%BUILD_DIR%\manifest-addition.txt"

echo.
echo === BUILD SUCCESSFUL ===
echo Output: %JAR_FILE%
echo.
echo To deploy:
echo   copy "%JAR_FILE%" "%ORIGINAL_JAR%"
echo.

endlocal
