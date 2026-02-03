#!/usr/bin/env python
"""
Tesseract OCR Setup Script

Downloads and sets up Tesseract OCR for bundled use with the project.
This allows the application to run without requiring users to install Tesseract separately.

Usage:
    python scripts/setup_tesseract.py

The script will:
1. Download Tesseract portable from GitHub
2. Extract to vendor/tesseract/
3. Verify the installation
"""

import os
import sys
import zipfile
import shutil
import urllib.request
from pathlib import Path


# Tesseract download URL (portable version for Windows)
# Using UB-Mannheim's builds which are well-maintained
TESSERACT_URL = "https://github.com/UB-Mannheim/tesseract/releases/download/v5.3.3/tesseract-ocr-w64-setup-5.3.3.20231005.exe"
TESSERACT_ZIP_URL = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.3.20231005.exe"

# Alternative: Use portable zip if available
PORTABLE_URL = "https://github.com/UB-Mannheim/tesseract/releases/download/v5.3.3/tesseract-ocr-w64-setup-5.3.3.20231005.exe"

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
VENDOR_DIR = PROJECT_ROOT / "vendor"
TESSERACT_DIR = VENDOR_DIR / "tesseract"


def download_file(url: str, dest: Path, desc: str = "Downloading") -> bool:
    """Download a file with progress indicator."""
    print(f"{desc}: {url}")
    print(f"Destination: {dest}")

    try:
        def progress_hook(count, block_size, total_size):
            percent = int(count * block_size * 100 / total_size) if total_size > 0 else 0
            sys.stdout.write(f"\r  Progress: {percent}%")
            sys.stdout.flush()

        urllib.request.urlretrieve(url, dest, progress_hook)
        print("\n  Download complete!")
        return True
    except Exception as e:
        print(f"\n  Download failed: {e}")
        return False


def setup_tesseract_manual():
    """
    Provide instructions for manual Tesseract setup.

    Since Tesseract doesn't have a simple portable zip, we'll guide users
    to install it and copy the files.
    """
    print("\n" + "=" * 60)
    print("TESSERACT OCR SETUP")
    print("=" * 60)

    # Create vendor directory
    TESSERACT_DIR.mkdir(parents=True, exist_ok=True)

    print("""
Tesseract OCR needs to be set up for this project.

OPTION 1: Automatic (if Tesseract is already installed)
---------------------------------------------------------
If you have Tesseract installed on your system, we can copy it:
""")

    # Check common installation paths
    common_paths = [
        Path(r"C:\Program Files\Tesseract-OCR"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR",
    ]

    found_path = None
    for path in common_paths:
        if path.exists() and (path / "tesseract.exe").exists():
            found_path = path
            break

    if found_path:
        print(f"Found Tesseract at: {found_path}")
        response = input("\nCopy this installation to vendor/tesseract? [Y/n]: ").strip().lower()

        if response != 'n':
            return copy_tesseract_installation(found_path)

    print("""
OPTION 2: Manual Installation
-----------------------------
1. Download Tesseract from:
   https://github.com/UB-Mannheim/tesseract/wiki

2. Install it (remember the installation path)

3. Run this script again, or manually copy:
   - tesseract.exe
   - All .dll files
   - tessdata/ folder (with eng.traineddata)

   To: {vendor_dir}

OPTION 3: Download Portable Version
------------------------------------
""".format(vendor_dir=TESSERACT_DIR))

    # Try to download portable if available
    response = input("Try to download and extract Tesseract? [Y/n]: ").strip().lower()
    if response != 'n':
        return download_and_setup_tesseract()

    return False


def copy_tesseract_installation(source_path: Path) -> bool:
    """Copy an existing Tesseract installation to vendor/."""
    print(f"\nCopying Tesseract from {source_path}...")

    try:
        # Remove existing if present
        if TESSERACT_DIR.exists():
            shutil.rmtree(TESSERACT_DIR)

        # Copy essential files
        TESSERACT_DIR.mkdir(parents=True, exist_ok=True)

        # Copy tesseract.exe
        shutil.copy2(source_path / "tesseract.exe", TESSERACT_DIR / "tesseract.exe")

        # Copy DLLs
        for dll in source_path.glob("*.dll"):
            shutil.copy2(dll, TESSERACT_DIR / dll.name)

        # Copy tessdata
        tessdata_src = source_path / "tessdata"
        tessdata_dst = TESSERACT_DIR / "tessdata"
        if tessdata_src.exists():
            shutil.copytree(tessdata_src, tessdata_dst)

        print("  Tesseract copied successfully!")
        return verify_installation()

    except Exception as e:
        print(f"  Error copying: {e}")
        return False


def download_and_setup_tesseract() -> bool:
    """
    Download Tesseract portable version.

    Note: Since there's no official portable zip, we'll create a minimal
    setup with just the required files.
    """
    print("\nSetting up Tesseract...")

    # Create directories
    TESSERACT_DIR.mkdir(parents=True, exist_ok=True)
    tessdata_dir = TESSERACT_DIR / "tessdata"
    tessdata_dir.mkdir(exist_ok=True)

    # Download eng.traineddata (the language file we need)
    traineddata_url = "https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata"
    traineddata_path = tessdata_dir / "eng.traineddata"

    if not traineddata_path.exists():
        print("\nDownloading English language data...")
        if not download_file(traineddata_url, traineddata_path, "Downloading eng.traineddata"):
            print("Failed to download language data.")
            return False

    print("""
Language data downloaded successfully!

However, the Tesseract executable needs to be installed separately.
Please install Tesseract from:
  https://github.com/UB-Mannheim/tesseract/wiki

Then run this script again to copy the installation.
""")

    return False


def verify_installation() -> bool:
    """Verify Tesseract installation."""
    print("\nVerifying installation...")

    tesseract_exe = TESSERACT_DIR / "tesseract.exe"
    tessdata = TESSERACT_DIR / "tessdata"
    eng_data = tessdata / "eng.traineddata"

    checks = [
        (tesseract_exe.exists(), f"tesseract.exe: {tesseract_exe}"),
        (tessdata.exists(), f"tessdata folder: {tessdata}"),
        (eng_data.exists(), f"eng.traineddata: {eng_data}"),
    ]

    all_ok = True
    for ok, desc in checks:
        status = "✓" if ok else "✗"
        print(f"  {status} {desc}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n✓ Tesseract setup complete!")

        # Test it works
        try:
            import subprocess
            result = subprocess.run(
                [str(tesseract_exe), "--version"],
                capture_output=True,
                text=True,
            )
            print(f"\nTesseract version:\n{result.stdout.strip()}")
        except Exception as e:
            print(f"\nWarning: Could not get version: {e}")
    else:
        print("\n✗ Setup incomplete. Please check the missing items.")

    return all_ok


def main():
    """Main entry point."""
    print("=" * 60)
    print("GDS2 RPA - Tesseract OCR Setup")
    print("=" * 60)

    # Check if already set up
    tesseract_exe = TESSERACT_DIR / "tesseract.exe"
    if tesseract_exe.exists():
        print(f"\nTesseract already exists at: {TESSERACT_DIR}")
        response = input("Verify existing installation? [Y/n]: ").strip().lower()
        if response != 'n':
            if verify_installation():
                return 0

        response = input("Re-setup Tesseract? [y/N]: ").strip().lower()
        if response != 'y':
            return 0

    # Run setup
    if setup_tesseract_manual():
        return 0
    else:
        print("\nSetup incomplete. Please follow the instructions above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
