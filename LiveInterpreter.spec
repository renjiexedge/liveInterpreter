# PyInstaller one-folder build (guide/distribution_plan.md Phase 4, §4.3).
# Build with build.ps1, which checks the result; by hand:
#   env/Scripts/python.exe -m PyInstaller LiveInterpreter.spec --noconfirm --clean
# Output: dist/LiveInterpreter/LiveInterpreter.exe plus _internal/ (bundled files
# land there, which is where config.paths.resource_path() looks when frozen).
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct, VSVersionInfo)

sys.path.insert(0, SPECPATH)
from version import BUILD, DISPLAY_VERSION, FILE_VERSION, __version__  # noqa: E402

# Only what the app reads at run time. Never .env, debug_dumps/, guide/, tests/.
datas = [
    ("ui/style.qss", "ui"),
    ("ui/assets", "ui/assets"),
    ("ui/fonts", "ui/fonts"),
    ("audio_test/loop_test.wav", "audio_test"),
]
datas += copy_metadata("keyring")      # backends are found through entry points
datas += copy_metadata("google-genai")

hiddenimports = [
    "PySide6.QtSvg",                   # SVG icons render blank without the plugin
    "win32ctypes.core",                # keyring's WinVaultKeyring backend
    *collect_submodules("keyring.backends"),
    # The SDK imports some modules lazily; its own test suite isn't needed.
    *collect_submodules("google.genai", filter=lambda name: ".tests" not in name),
    *collect_submodules("websockets"),
]

excludes = [
    "tkinter", "dotenv", "pytest", "_pytest", "pytest_asyncio", "run_cli", "tests", "installer",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
    "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf",
]

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=FILE_VERSION, prodvers=FILE_VERSION),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Xedge"),
            StringStruct("FileDescription", "Xedge Live Interpreter"),
            StringStruct("FileVersion", __version__),
            StringStruct("InternalName", "LiveInterpreter"),
            StringStruct("OriginalFilename", "LiveInterpreter.exe"),
            StringStruct("ProductName", "Xedge Live Interpreter"),
            StringStruct("ProductVersion", f"{DISPLAY_VERSION} (build {BUILD})"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveInterpreter",
    console=False,
    icon="ui/assets/app.ico",
    upx=False,  # UPX-packed exes trip antivirus heuristics
    version=version_info,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="LiveInterpreter")
