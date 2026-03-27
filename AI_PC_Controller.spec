# -*- mode: python ; coding: utf-8 -*-
# Сборка JARVIS (AI_PC_Controller) в один exe

block_cipher = None

from PyInstaller.utils.hooks import copy_metadata

# Метаданные пакетов нужны в exe для importlib.metadata (replicate берёт версию при импорте)
datas_extra = copy_metadata('replicate')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('games.json', '.'),
    ] + datas_extra,
    hiddenimports=[
        'config',
        'neural_network_manager',
        'replicate_manager',
        'dotenv',
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.QtMultimedia',
        'replicate',
        'requests',
        'telegram',
        'telethon',
        'psutil',
        'pyautogui',
        'PIL',
        'PIL.Image',
        'speech_recognition',
        'darkdetect',
        'speedtest',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'torch', 'tensorflow', 'transformers', 'sklearn', 'matplotlib', 'scipy', 'pandas', 'cv2', 'torchvision', 'torchaudio'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='JARVIS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
