"""Build the macOS app alongside the project's .venv environment."""
import pathlib, platform, plistlib, shutil, subprocess, sys
root = pathlib.Path(__file__).resolve().parent.parent
app = root / 'reZme.app'
contents = app / 'Contents'
resources = contents / 'Resources'
(resources).mkdir(parents=True, exist_ok=True)
(contents / 'MacOS').mkdir(exist_ok=True)
subprocess.run(['xcrun', 'swiftc', str(root/'desktop/ReZme.swift'), '-o', str(contents/'MacOS/reZme'), '-target', f'{platform.machine()}-apple-macos14.0', '-framework', 'SwiftUI', '-framework', 'AppKit', '-parse-as-library'], check=True)
for name, source in [('worker.py', root/'desktop/worker.py'), ('yt_digest.py', root/'yt_digest.py')]:
    shutil.copy2(source, resources/name)
(resources/'runtime.txt').unlink(missing_ok=True)  # Remove legacy machine-specific configuration.
iconset = root/'desktop/AppIcon.iconset'
iconset.mkdir(exist_ok=True)
source = root/'desktop/icon.png'
subprocess.run(['swift', str(root/'desktop/make_icon.swift'), str(source)], check=True)
for size in [16,32,128,256,512]:
    for scale in [1,2]:
        target = iconset/f'icon_{size}x{size}{"@2x" if scale==2 else ""}.png'
        subprocess.run(['sips','-z',str(size*scale),str(size*scale),str(source),'--out',str(target)], stdout=subprocess.DEVNULL, check=True)
subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(resources/'AppIcon.icns')],check=True)
with (contents/'Info.plist').open('wb') as f:
    plistlib.dump({'CFBundleExecutable':'reZme','CFBundleIdentifier':'com.almatechnologies.rezme', 'CFBundleName':'reZme', 'CFBundleDisplayName':'reZme','CFBundlePackageType':'APPL','CFBundleShortVersionString':'1.0','CFBundleVersion':'1','CFBundleIconFile':'AppIcon','NSHighResolutionCapable':True,'LSMinimumSystemVersion':'14.0'},f)
subprocess.run(['codesign','--force','--deep','--sign','-',str(app)],check=True)
print(app)
