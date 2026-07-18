# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['text_transcript/lyric_extractor', 'chords_transcript'],
    binaries=[],
    datas=[
        ('../frontend/dist/frontend/browser', 'frontend/dist/frontend/browser'),
        ('ffmpeg', 'ffmpeg'),
        ('chords_transcript/chords_extractor/essentia', 'chords_transcript/chords_extractor/essentia'),
    ],
    hiddenimports=[
        'whisper',
        'torch',
        'madmom',
        'librosa',
        'soundfile',
        'yt_dlp',
        'numpy',
        'scipy',
        'sklearn',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VidiChord',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VidiChord',
)
